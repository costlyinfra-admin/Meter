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
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api, ApiError, type AiApplication, type AiTrace, type AiTracePage } from "../api";
import { compact, duration, money, num, sinceNow } from "../format";
import { DEFAULT_WINDOW_DAYS, TRACE_WINDOWS, daysFromParams } from "./traceWindow";

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

function EmptyTraces({ filtered }: { filtered: boolean }) {
  // A filtered view finding nothing is not the same as having no traces, and
  // sending someone to Install SDK when they simply typed a narrow search
  // would be telling them their working setup is broken.
  if (filtered)
    return (
      <div className="empty-state">
        <p className="empty-title">No runs match these filters</p>
        <p className="muted">
          Widen the window, clear the search, or choose a different application.
        </p>
      </div>
    );
  return (
    <div className="empty-state">
      <p className="empty-title">No traces received</p>
      <p className="muted">Run an instrumented AI workflow to see request-level cost here.</p>
      <Link className="button-link" to="/install-sdk">
        Install SDK
      </Link>
    </div>
  );
}

/** A row that opens the run it describes, the way a feature row opens a feature. */
function TraceRow({ trace, children }: { trace: AiTrace; children: React.ReactNode }) {
  const navigate = useNavigate();
  return (
    <tr
      className="feature-row"
      onClick={() => navigate(`/traces/${encodeURIComponent(trace.trace_id)}`)}
    >
      {children}
    </tr>
  );
}

/** The workflow name as the row's link — the cell someone aims at. */
function WorkflowCell({ trace }: { trace: AiTrace }) {
  return (
    <td>
      <Link
        to={`/traces/${encodeURIComponent(trace.trace_id)}`}
        onClick={(e) => e.stopPropagation()}
      >
        {trace.operation_name}
      </Link>
    </td>
  );
}

function TraceTable({ traces }: { traces: AiTrace[] }) {
  return (
    <div className="table-scroll">
      <table className="features-table">
        <caption className="sr-only">AI workflow runs, newest first</caption>
        <thead>
          <tr>
            <th>Workflow</th>
            <th>Application</th>
            <th>Feature</th>
            <th>Started</th>
            <th className="num">Model calls</th>
            <th className="num">Steps</th>
            <th className="num">Tokens</th>
            <th className="num">Cost</th>
            <th className="num">Duration</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {traces.map((t) => (
            <TraceRow key={t.id} trace={t}>
              <WorkflowCell trace={t} />
              <td>{t.application.name}</td>
              <td>{t.feature?.name ?? <span className="muted">Unattributed</span>}</td>
              <td>{sinceNow(t.started_at)}</td>
              <td className="num">{num(t.llm_calls)}</td>
              <td className="num">{num(t.span_count)}</td>
              <td className="num">{compact(t.total_tokens)}</td>
              <td className="num">{money(t.total_cost)}</td>
              <td className="num">{duration(t.duration_ms)}</td>
              <td>
                <TraceStatus status={t.live_status} />
              </td>
            </TraceRow>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function RunningTable({ traces }: { traces: AiTrace[] }) {
  return (
    <div className="table-scroll">
      <table className="features-table">
        <caption className="sr-only">Agent runs currently active</caption>
        <thead>
          <tr>
            <th>Workflow</th>
            <th>Application</th>
            <th>Feature</th>
            <th>Current step</th>
            {/* No "Started" column: Runtime is derived from it and answers the
                question this tab exists for — how long has this been going —
                and the twelfth column pushed Status, the whole point of the
                view, off the right edge. */}
            <th>Last activity</th>
            <th className="num">Runtime</th>
            <th className="num">Steps</th>
            <th className="num">Model calls</th>
            <th className="num">Tokens</th>
            <th className="num">Cost so far</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {traces.map((t) => (
            <TraceRow key={t.id} trace={t}>
              <WorkflowCell trace={t} />
              <td>{t.application.name}</td>
              <td>{t.feature?.name ?? <span className="muted">Unattributed</span>}</td>
              <td>{t.current_span_id ?? <span className="muted">—</span>}</td>
              <td>{sinceNow(t.last_activity_at)}</td>
              <td className="num">{duration(Date.now() - new Date(t.started_at).getTime())}</td>
              <td className="num">{num(t.span_count)}</td>
              <td className="num">{num(t.llm_calls)}</td>
              <td className="num">{compact(t.total_tokens)}</td>
              <td className="num">{money(t.total_cost)}</td>
              <td>
                <TraceStatus status={t.live_status} />
              </td>
            </TraceRow>
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
  total,
}: {
  traces: AiTrace[];
  dimension: string;
  onDimension: (id: string) => void;
  total: number;
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
        <table className="features-table">
          <caption className="sr-only">Trace cost grouped by {dim.label}</caption>
          <thead>
            <tr>
              <th>{dim.label}</th>
              <th className="num">Cost</th>
              <th className="num">Runs</th>
              <th className="num">Cost / run</th>
              <th className="num">Model calls</th>
              <th className="num">Tokens</th>
              <th className="num">Avg steps</th>
              <th className="num">Error rate</th>
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
                <td className="num">{money(g.spend)}</td>
                <td className="num">{num(g.runs)}</td>
                <td className="num">{money(g.spend / g.runs)}</td>
                <td className="num">{num(g.calls)}</td>
                <td className="num">{compact(g.tokens)}</td>
                <td className="num">{(g.steps / g.runs).toFixed(1)}</td>
                <td className="num">{((g.errors / g.runs) * 100).toFixed(1)}%</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {/* Explore groups the rows it has, not the whole window. Saying so
          matters: without it these totals read as complete, and someone
          comparing them against the run counts on Applications would find
          smaller numbers and no explanation. */}
      <p className="muted legend">
        {total > traces.length
          ? `Grouped from the ${num(traces.length)} most recent runs of ${num(total)} in this window — totals here are a sample, not the period's full spend.`
          : `Grouped from all ${num(traces.length)} runs in this window.`}
      </p>
    </div>
  );
}

export function TracesPage() {
  const [params, setParams] = useSearchParams();
  const tabParam = params.get("tab");
  const tab: TabId = isTab(tabParam) ? tabParam : "all";
  const sort = params.get("sort") ?? "newest";
  const dimension = params.get("group") ?? "application";
  const days = daysFromParams(params);
  const applicationId = params.get("application_id") ?? "";
  const q = params.get("q") ?? "";

  const [page, setPage] = useState<AiTracePage | null>(null);
  const [applications, setApplications] = useState<AiApplication[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  // The typed text, kept separate from the URL so every keystroke does not
  // become a request or a history entry. Committed on a short debounce.
  const [draft, setDraft] = useState(q);
  // A poll must never race itself: an in-flight request means skip this tick.
  const inFlight = useRef(false);

  const set = (next: Record<string, string>) => {
    const merged = new URLSearchParams(params);
    for (const [k, v] of Object.entries(next)) {
      if (v) merged.set(k, v);
      else merged.delete(k); // an empty filter is no filter, not `?q=`
    }
    setParams(merged, { replace: true });
  };

  // The application filter's options. A failure here costs the dropdown, not
  // the page — the traces below are what someone came for.
  useEffect(() => {
    api
      .aiApplications({ days })
      .then((r) => setApplications(r.applications))
      .catch(() => setApplications([]));
  }, [days]);

  // Keep the box in step when the URL changes underneath it (back button, or a
  // link into a filtered view), without fighting what is being typed.
  useEffect(() => setDraft(q), [q]);

  useEffect(() => {
    if (draft === q) return;
    const timer = window.setTimeout(() => set({ q: draft }), 300);
    return () => window.clearTimeout(timer);
    // `set` closes over the current params; re-running on every param change
    // would restart the debounce, so this deliberately watches the text only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft, q]);

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
          // Server-side, so the count under the table is the truth about the
          // whole result and not just the page that happens to be loaded.
          q: q || undefined,
          application_id: applicationId || undefined,
          days,
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
    [tab, sort, q, applicationId, days],
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
  // Whether the reader has narrowed anything. Drives what an empty result says:
  // "nothing matches" is a different message from "nothing has ever arrived".
  const filtered = Boolean(q || applicationId || days !== DEFAULT_WINDOW_DAYS);

  return (
    <div className="content">
      <div className="dash-head">
        <div>
          <h1>Traces</h1>
          <p className="muted dash-sub">Request-level evidence behind your AI spend.</p>
        </div>
        <div className="period-controls detail-period">
          <span className="muted period-label">Showing</span>
          <select
            className="period-select"
            aria-label="Time window"
            value={days}
            onChange={(e) => set({ days: e.target.value })}
          >
            {TRACE_WINDOWS.map((w) => (
              <option key={w.days} value={w.days}>
                {w.label}
              </option>
            ))}
          </select>
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
        {/* Search sits in the tab bar, as it does on the Overview's by-feature
            table. It filters on the server, so it narrows every run in the
            window rather than only the fifty already on screen. */}
        <input
          type="search"
          className="tab-search"
          value={draft}
          placeholder="Search workflows…"
          aria-label="Search workflows"
          onChange={(e) => setDraft(e.target.value)}
        />
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
              Application
              <select
                value={applicationId}
                onChange={(e) => set({ application_id: e.target.value })}
              >
                <option value="">All applications</option>
                {applications.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.name}
                  </option>
                ))}
              </select>
            </label>
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
            <EmptyTraces filtered={filtered} />
          ) : (
            <>
              <TraceTable traces={traces} />
              <p className="muted legend">
                {page && page.total > traces.length
                  ? `Showing the first ${num(traces.length)} of ${num(page.total)} runs — narrow the window, application or search to see fewer. `
                  : ""}
                A run marked stale has gone quiet longer than your threshold; it has not failed and
                may still finish.
              </p>
            </>
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
            <EmptyTraces filtered={filtered} />
          ) : (
            <Explore
              traces={traces}
              dimension={dimension}
              onDimension={(id) => set({ group: id })}
              total={page?.total ?? traces.length}
            />
          )}
        </section>
      )}
    </div>
  );
}
