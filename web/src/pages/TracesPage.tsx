/**
 * Traces — the request-level evidence behind the spend on every other page.
 *
 * Three tabs over one dataset. "All traces" is the history, "Running now" is the
 * same table filtered to live agents and polled, "Explore" groups the same rows
 * by a dimension. They share filter state through the URL so a link to a
 * filtered view is a link someone can send.
 *
 * Two things this page is careful about:
 *
 * **Stale is a presentation, not a failure.** A quiet agent is still `running`
 * in the database and may still finish. It gets its own badge, distinct from
 * error, and the status is never communicated by colour alone — every badge
 * carries its word.
 *
 * **Polling must not lie or leak.** Running now refreshes only while the tab is
 * selected AND the document is visible, never overlaps a request with itself,
 * and on a network failure keeps showing the last good data rather than
 * blanking the table. A poll is a read; it can never create an event.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, ApiError, type AiTrace, type AiTracePage } from "../api";
import { duration, money, num, sinceNow } from "../format";

const TABS = [
  { id: "all", label: "All traces" },
  { id: "running", label: "Running now" },
  { id: "explore", label: "Explore" },
] as const;
type TabId = (typeof TABS)[number]["id"];

const SORTS = [
  { id: "newest", label: "Newest" },
  { id: "expensive", label: "Most expensive" },
  { id: "tokens", label: "Most tokens" },
  { id: "slowest", label: "Slowest" },
  { id: "steps", label: "Most steps" },
] as const;

/** How often Running now re-reads. Slow enough not to hammer the API, fast
 *  enough that a person watching an agent sees it move. */
const POLL_MS = 5000;

function isTab(value: string | null): value is TabId {
  return TABS.some((t) => t.id === value);
}

/** Status as a badge. The word is always present: colour alone would exclude
 *  anyone who cannot distinguish these hues, and "stale" vs "error" is exactly
 *  the distinction that matters most here. */
export function TraceStatus({ status }: { status: AiTrace["live_status"] }) {
  const label = {
    running: "Running",
    stale: "Stale",
    success: "Success",
    error: "Error",
    cancelled: "Cancelled",
  }[status];
  return (
    <span className={`badge trace-status trace-status-${status}`}>
      {status === "running" && <span className="trace-pulse" aria-hidden />}
      {label}
    </span>
  );
}

function EmptyTraces() {
  return (
    <div className="trace-empty">
      <h2>No traces received</h2>
      <p className="muted">Run an instrumented AI workflow to see request-level cost here.</p>
      <Link className="button-link" to="/install-sdk">
        Install SDK
      </Link>
    </div>
  );
}

function TraceTable({ traces }: { traces: AiTrace[] }) {
  return (
    <div className="table-scroll">
      <table className="data-table trace-table">
        <caption className="sr-only">AI workflow runs, newest first</caption>
        <thead>
          <tr>
            <th scope="col">Time</th>
            <th scope="col">Application</th>
            <th scope="col">Feature</th>
            <th scope="col">Workflow</th>
            <th scope="col">Model calls</th>
            <th scope="col">Steps</th>
            <th scope="col">Tokens</th>
            <th scope="col">Cost</th>
            <th scope="col">Duration</th>
            <th scope="col">Status</th>
          </tr>
        </thead>
        <tbody>
          {traces.map((t) => (
            <tr key={t.id}>
              <td>{sinceNow(t.started_at)}</td>
              <td>{t.application.name}</td>
              <td>{t.feature?.name ?? "Unattributed"}</td>
              <td>
                <Link to={`/traces/${encodeURIComponent(t.trace_id)}`}>{t.operation_name}</Link>
              </td>
              <td className="numeric">{num(t.llm_calls)}</td>
              <td className="numeric">{num(t.span_count)}</td>
              <td className="numeric">{num(t.total_tokens)}</td>
              <td className="numeric">{money(t.total_cost)}</td>
              <td className="numeric">{duration(t.duration_ms)}</td>
              <td>
                <TraceStatus status={t.live_status} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function RunningTable({ traces }: { traces: AiTrace[] }) {
  return (
    <div className="table-scroll">
      <table className="data-table trace-table">
        <caption className="sr-only">Agent runs currently active</caption>
        <thead>
          <tr>
            <th scope="col">Application</th>
            <th scope="col">Feature</th>
            <th scope="col">Agent/workflow</th>
            <th scope="col">Status</th>
            <th scope="col">Current operation</th>
            <th scope="col">Started</th>
            <th scope="col">Last activity</th>
            <th scope="col">Runtime</th>
            <th scope="col">Steps</th>
            <th scope="col">LLM calls</th>
            <th scope="col">Tokens</th>
            <th scope="col">Cost so far</th>
          </tr>
        </thead>
        <tbody>
          {traces.map((t) => (
            <tr key={t.id}>
              <td>{t.application.name}</td>
              <td>{t.feature?.name ?? "Unattributed"}</td>
              <td>
                <Link to={`/traces/${encodeURIComponent(t.trace_id)}`}>{t.operation_name}</Link>
              </td>
              <td>
                <TraceStatus status={t.live_status} />
              </td>
              <td>{t.current_span_id ?? "—"}</td>
              <td>{sinceNow(t.started_at)}</td>
              <td>{sinceNow(t.last_activity_at)}</td>
              <td className="numeric">{duration(Date.now() - new Date(t.started_at).getTime())}</td>
              <td className="numeric">{num(t.span_count)}</td>
              <td className="numeric">{num(t.llm_calls)}</td>
              <td className="numeric">{num(t.total_tokens)}</td>
              <td className="numeric">{money(t.total_cost)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Explore: the same rows, grouped. Deliberately not a dashboard designer —
 *  one dimension, the measures that matter, and a shareable URL. */
const DIMENSIONS = [
  { id: "application", label: "Application", of: (t: AiTrace) => t.application.name },
  { id: "feature", label: "Feature", of: (t: AiTrace) => t.feature?.name ?? "Unattributed" },
  { id: "workflow", label: "Workflow", of: (t: AiTrace) => t.operation_name },
  { id: "environment", label: "Environment", of: (t: AiTrace) => t.environment },
  { id: "release", label: "Release", of: (t: AiTrace) => t.release_version ?? "—" },
  { id: "status", label: "Status", of: (t: AiTrace) => t.live_status },
] as const;

function Explore({
  traces,
  dimension,
  onDimension,
}: {
  traces: AiTrace[];
  dimension: string;
  onDimension: (id: string) => void;
}) {
  const dim = DIMENSIONS.find((d) => d.id === dimension) ?? DIMENSIONS[0];
  const groups = new Map<
    string,
    { runs: number; spend: number; tokens: number; calls: number; errors: number; steps: number }
  >();
  for (const t of traces) {
    const key = dim.of(t);
    const g = groups.get(key) ?? { runs: 0, spend: 0, tokens: 0, calls: 0, errors: 0, steps: 0 };
    g.runs += 1;
    g.spend += t.total_cost;
    g.tokens += t.total_tokens;
    g.calls += t.llm_calls;
    g.steps += t.span_count;
    if (t.live_status === "error") g.errors += 1;
    groups.set(key, g);
  }
  const rows = [...groups.entries()].sort((a, b) => b[1].spend - a[1].spend);
  const max = rows[0]?.[1].spend || 1;

  return (
    <div className="explore">
      <label className="explore-dimension">
        Group by
        <select value={dim.id} onChange={(e) => onDimension(e.target.value)}>
          {DIMENSIONS.map((d) => (
            <option key={d.id} value={d.id}>
              {d.label}
            </option>
          ))}
        </select>
      </label>
      <div className="table-scroll">
        <table className="data-table">
          <caption className="sr-only">Trace cost grouped by {dim.label}</caption>
          <thead>
            <tr>
              <th scope="col">{dim.label}</th>
              <th scope="col">Cost</th>
              <th scope="col">Runs</th>
              <th scope="col">Cost per run</th>
              <th scope="col">LLM calls</th>
              <th scope="col">Tokens</th>
              <th scope="col">Avg steps</th>
              <th scope="col">Error rate</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(([key, g]) => (
              <tr key={key}>
                <td>
                  {/* A bar in the cell rather than a chart library: the shape is
                      the comparison, and this page already has a table. */}
                  <span className="explore-bar" aria-hidden>
                    <span style={{ width: `${Math.max(2, (g.spend / max) * 100)}%` }} />
                  </span>
                  {key}
                </td>
                <td className="numeric">{money(g.spend)}</td>
                <td className="numeric">{num(g.runs)}</td>
                <td className="numeric">{money(g.spend / g.runs)}</td>
                <td className="numeric">{num(g.calls)}</td>
                <td className="numeric">{num(g.tokens)}</td>
                <td className="numeric">{(g.steps / g.runs).toFixed(1)}</td>
                <td className="numeric">{((g.errors / g.runs) * 100).toFixed(1)}%</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function TracesPage() {
  const [params, setParams] = useSearchParams();
  const tabParam = params.get("tab");
  const tab: TabId = isTab(tabParam) ? tabParam : "all";
  const sort = params.get("sort") ?? "newest";
  const dimension = params.get("group") ?? "application";

  const [page, setPage] = useState<AiTracePage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  // A poll must never race itself: an in-flight request means skip this tick.
  const inFlight = useRef(false);

  const set = (next: Record<string, string>) => {
    const merged = new URLSearchParams(params);
    for (const [k, v] of Object.entries(next)) merged.set(k, v);
    setParams(merged, { replace: true });
  };

  const load = useCallback(
    async (quiet: boolean) => {
      if (inFlight.current) return;
      inFlight.current = true;
      if (!quiet) setLoading(true);
      try {
        const next = await api.aiTraces({
          sort: tab === "running" ? "newest" : sort,
          // Everything still stored as `running` — which includes the ones the
          // server will label stale. Filtering on the activity cutoff here
          // would hide exactly the agents someone opened this tab to find.
          trace_status: tab === "running" ? "running" : undefined,
          limit: tab === "explore" ? 200 : 50,
        });
        setPage(next);
        setError(null);
      } catch (err) {
        // A failed poll must not erase data that was valid a moment ago.
        if (!quiet) setError(err instanceof ApiError ? err.message : "Could not load traces.");
      } finally {
        inFlight.current = false;
        setLoading(false);
      }
    },
    [tab, sort],
  );

  useEffect(() => {
    load(false);
  }, [load]);

  useEffect(() => {
    if (tab !== "running") return;
    // Only while this tab is showing AND the document is visible: polling a
    // hidden tab spends the customer's API budget on nothing.
    const tick = () => {
      if (document.visibilityState === "visible") load(true);
    };
    const timer = window.setInterval(tick, POLL_MS);
    document.addEventListener("visibilitychange", tick);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", tick);
    };
  }, [tab, load]);

  const traces = page?.traces ?? [];

  return (
    <div className="content">
      <div className="dash-head">
        <div>
          <h1>Traces</h1>
          <p className="muted dash-sub">Request-level evidence behind your AI spend.</p>
        </div>
      </div>

      <div className="tabs tabs-scroll" role="tablist" aria-label="Trace views">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            aria-selected={tab === t.id}
            className={tab === t.id ? "tab active" : "tab"}
            onClick={() => set({ tab: t.id })}
          >
            {t.label}
          </button>
        ))}
      </div>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      {tab === "all" && (
        <section className="source-section" role="tabpanel">
          <div className="trace-filters">
            <label>
              Sort
              <select value={sort} onChange={(e) => set({ sort: e.target.value })}>
                {SORTS.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.label}
                  </option>
                ))}
              </select>
            </label>
            {page && (
              <span className="muted trace-count">
                {num(page.total)} run{page.total === 1 ? "" : "s"}
              </span>
            )}
          </div>
          {loading && !page ? (
            <p className="muted">Loading traces…</p>
          ) : traces.length === 0 ? (
            <EmptyTraces />
          ) : (
            <TraceTable traces={traces} />
          )}
        </section>
      )}

      {tab === "running" && (
        <section className="source-section" role="tabpanel">
          <p className="muted">
            Agents currently executing. A run that has gone quiet longer than your stale threshold
            is marked stale — it has not failed, and may still finish.
          </p>
          {loading && !page ? (
            <p className="muted">Loading…</p>
          ) : traces.length === 0 ? (
            <p className="muted trace-empty-inline">No agent runs are active.</p>
          ) : (
            <RunningTable traces={traces} />
          )}
        </section>
      )}

      {tab === "explore" && (
        <section className="source-section" role="tabpanel">
          {loading && !page ? (
            <p className="muted">Loading…</p>
          ) : traces.length === 0 ? (
            <EmptyTraces />
          ) : (
            <Explore
              traces={traces}
              dimension={dimension}
              onDimension={(id) => set({ group: id })}
            />
          )}
        </section>
      )}
    </div>
  );
}
