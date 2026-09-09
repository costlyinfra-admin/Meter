/**
 * One agent run: what it did, in what order, and what each step cost.
 *
 * The waterfall is built from `parent_span_id` rather than a nested payload,
 * because spans arrive independently and out of order — a child can be stored
 * before its parent, and a parent's event can be lost entirely. A span whose
 * parent never arrived is rendered as a root and labelled, rather than dropped:
 * it happened, and it cost money.
 *
 * Depth is drawn with indentation and a bar, not a charting library. The bar is
 * proportional to the span's share of the run's duration, which is the only
 * comparison this view needs.
 */
import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, ApiError, type AiSpan, type AiTrace } from "../api";
import { compact, duration, money, num, sinceNow } from "../format";
import { TraceStatus } from "./TracesPage";

const KIND_LABEL: Record<AiSpan["span_kind"], string> = {
  workflow: "Workflow",
  llm: "LLM",
  embedding: "Embedding",
  retrieval: "Retrieval",
  tool: "Tool",
  guardrail: "Guardrail",
  evaluation: "Evaluation",
};

interface Node {
  span: AiSpan;
  depth: number;
}

/** Flatten the parent/child graph into render order.
 *
 *  Cycles and unresolved parents are both possible in data that arrives over a
 *  network in any order, so this walks from roots and tracks what it has
 *  emitted — anything left over is appended rather than lost. */
function order(spans: AiSpan[]): Node[] {
  const children = new Map<string, AiSpan[]>();
  const ids = new Set(spans.map((s) => s.external_span_id));
  const roots: AiSpan[] = [];
  for (const span of spans) {
    const parent = span.parent_span_id;
    if (!parent || !ids.has(parent)) {
      roots.push(span);
      continue;
    }
    children.set(parent, [...(children.get(parent) ?? []), span]);
  }
  const out: Node[] = [];
  const seen = new Set<string>();
  const walk = (span: AiSpan, depth: number) => {
    if (seen.has(span.external_span_id)) return;
    seen.add(span.external_span_id);
    out.push({ span, depth });
    for (const child of children.get(span.external_span_id) ?? []) walk(child, depth + 1);
  };
  for (const root of roots) walk(root, 0);
  // Anything a cycle kept us from reaching still gets shown.
  for (const span of spans) if (!seen.has(span.external_span_id)) out.push({ span, depth: 0 });
  return out;
}

function SpanRow({ node, start, span: total }: { node: Node; start: number; span: number }) {
  const [open, setOpen] = useState(false);
  const { span, depth } = node;
  const began = new Date(span.started_at).getTime();
  const ended = span.ended_at ? new Date(span.ended_at).getTime() : began;
  const offset = total > 0 ? ((began - start) / total) * 100 : 0;
  const width = total > 0 ? Math.max(1.5, ((ended - began) / total) * 100) : 2;

  const tokens = [
    ["Input", span.tokens_in],
    ["Output", span.tokens_out],
    ["Cache read", span.cache_read_tokens],
    ["Cache write", span.cache_write_tokens],
    ["Reasoning", span.reasoning_tokens],
  ].filter(([, v]) => v !== null && v !== undefined) as [string, number][];

  return (
    <li className={`span-row span-${span.status}`}>
      <button
        type="button"
        className="span-head"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <span className="span-name" style={{ paddingLeft: `${depth * 1.1}rem` }}>
          <span className="span-caret" aria-hidden>
            {open ? "▾" : "▸"}
          </span>
          {span.operation_name}
          <span className="span-kind">{KIND_LABEL[span.span_kind]}</span>
          {span.parent_missing && (
            <span className="span-orphan" title="This step's parent event never arrived">
              parent missing
            </span>
          )}
        </span>
        <span className="span-track" aria-hidden>
          <span className="span-bar" style={{ marginLeft: `${offset}%`, width: `${width}%` }} />
        </span>
        <span className="span-meta">
          {span.model && <span className="span-model">{span.model}</span>}
          <span className="numeric">{span.amount > 0 ? money(span.amount) : "—"}</span>
          <span className="numeric">{duration(span.latency_ms)}</span>
          <TraceStatus status={span.status} />
        </span>
      </button>
      {open && (
        <div className="span-detail">
          <dl>
            {tokens.map(([label, value]) => (
              <div key={label}>
                <dt>{label} tokens</dt>
                <dd className="numeric">{num(value)}</dd>
              </div>
            ))}
            {span.provider && (
              <div>
                <dt>Provider</dt>
                <dd>{span.provider}</dd>
              </div>
            )}
            {span.prompt_id && (
              <div>
                <dt>Prompt</dt>
                <dd>
                  {span.prompt_id}
                  {span.prompt_version && ` v${span.prompt_version}`}
                </dd>
              </div>
            )}
            <div>
              <dt>Span ID</dt>
              <dd className="span-id">{span.external_span_id}</dd>
            </div>
          </dl>
          {/* Said explicitly, because its absence is a deliberate guarantee
              rather than a gap someone should file a bug about. */}
          <p className="muted span-privacy">
            Meter records prompt identity, tokens and cost — never prompt or response content.
          </p>
        </div>
      )}
    </li>
  );
}

export function TraceDetail() {
  const { id = "" } = useParams();
  const [trace, setTrace] = useState<AiTrace | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    try {
      setTrace(await api.aiTrace(id));
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load this trace.");
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    load();
  }, [load]);

  // A running trace is still changing, so this view refreshes itself — but only
  // while it is running, and only while the tab is visible.
  const live = trace?.live_status === "running" || trace?.live_status === "stale";
  useEffect(() => {
    if (!live) return;
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") load();
    }, 5000);
    return () => window.clearInterval(timer);
  }, [live, load]);

  if (loading)
    return (
      <div className="content">
        <p className="muted">Loading trace…</p>
      </div>
    );
  if (error || !trace) {
    return (
      <div className="content">
        <p className="error" role="alert">
          {error ?? "Trace not found."}
        </p>
        <Link className="link breadcrumb" to="/traces">
          ← All traces
        </Link>
      </div>
    );
  }

  const spans = trace.spans ?? [];
  const start = spans.length
    ? Math.min(...spans.map((s) => new Date(s.started_at).getTime()))
    : new Date(trace.started_at).getTime();
  const finish = spans.length
    ? Math.max(...spans.map((s) => new Date(s.ended_at ?? s.started_at).getTime()))
    : Date.now();
  const total = Math.max(finish - start, 1);
  const runtime = trace.duration_ms ?? Date.now() - new Date(trace.started_at).getTime();

  return (
    <div className="content">
      <Link to="/traces" className="link breadcrumb">
        ← All traces
      </Link>

      <h1>{trace.operation_name}</h1>
      <p className="detail-meta">
        <TraceStatus status={trace.live_status} />
        <Link to={`/applications?days=30`}>{trace.application.name}</Link>
        {trace.feature ? (
          <Link to={`/features/${trace.feature.id}`}>{trace.feature.name}</Link>
        ) : (
          <span className="muted">Unattributed</span>
        )}
        <span className="muted">{trace.environment}</span>
        {trace.release_version && <code className="slug-tag">{trace.release_version}</code>}
        <span className="muted">started {sinceNow(trace.started_at)}</span>
      </p>

      {live && (
        <p className="muted trace-live-note">
          Currently running
          {trace.current_span_id && (
            <>
              {" "}
              · step <code>{trace.current_span_id}</code>
            </>
          )}{" "}
          · last activity {sinceNow(trace.last_activity_at)} · cost so far {money(trace.total_cost)}
        </p>
      )}

      <section className="detail-section">
        <div className="section-head">
          <div>
            <h2>Steps</h2>
            <span className="section-sub muted">
              Every step this run took, in the order it took them. The bar is when the step ran
              within the run.
            </span>
          </div>
          <div className="section-stats">
            <span>
              <strong>{money(trace.total_cost)}</strong> {live ? "so far" : "total"}
            </span>
            <span>
              <strong>{duration(runtime)}</strong> {live ? "runtime" : "duration"}
            </span>
            <span>
              <strong>{num(trace.span_count)}</strong> step{trace.span_count === 1 ? "" : "s"}
            </span>
            <span>
              <strong>{num(trace.llm_calls)}</strong> model call
              {trace.llm_calls === 1 ? "" : "s"}
            </span>
            <span>
              <strong>{compact(trace.total_tokens)}</strong> tokens
            </span>
          </div>
        </div>
        {spans.length === 0 ? (
          <p className="muted">No steps recorded for this run yet.</p>
        ) : (
          <>
            <ul className="span-list">
              {order(spans).map((node) => (
                <SpanRow key={node.span.external_span_id} node={node} start={start} span={total} />
              ))}
            </ul>
            <p className="muted legend">
              Meter records what each step cost and how long it took — never the prompt, the
              response, the tool arguments or the documents retrieved.
            </p>
          </>
        )}
      </section>
    </div>
  );
}
