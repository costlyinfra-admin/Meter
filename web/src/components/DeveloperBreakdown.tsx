/**
 * Overview "By Developer" tab — build (make) cost attributed to each developer,
 * broken down by the coding tool they used, over the Overview's selected review
 * period. Build cost is the only cost tied to a person, so this view is build-only
 * (inference has no developer) — never blended (invariant 2). Rows with no
 * developer land in an Unattributed bucket so the parts reconcile to the total.
 */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError, type DiscoveryScope, type ProviderSpend, type ReviewRange } from "../api";
import { compact, money, num, prettyTool, unitMoney } from "../format";
import { SpendBars } from "./SpendBars";
import { TrendChart } from "./TrendChart";

export function DeveloperBreakdown({
  range,
  refreshKey = 0,
}: {
  range: ReviewRange;
  /** Bumped by the Overview's refresh control to re-pull this breakdown. */
  refreshKey?: number;
}) {
  const [data, setData] = useState<ProviderSpend | null>(null);
  // Bumped when a discovery run finishes here, so the tables reflect what it
  // just collected without the reader having to reload the page.
  const [reload, setReload] = useState(0);

  useEffect(() => {
    let active = true;
    setData(null);
    api
      .providerSpend(range)
      .then((d) => active && setData(d))
      .catch(() => active && setData(null));
    return () => {
      active = false;
    };
  }, [range, refreshKey, reload]);

  return (
    <>
      <div className="section-head breakdown-head">
        <div>
          <h2>Build cost by developer</h2>
          <span className="section-sub muted">
            Who spent what on AI coding tools — build (make) cost only, never blended with run cost.
          </span>
        </div>
      </div>

      {data === null ? (
        <p className="muted">Loading…</p>
      ) : data.build_by_developer.length === 0 ? (
        <div className="empty-state">
          <p className="empty-title">No build cost yet</p>
          <p className="muted">
            Import a coding-tool spend CSV or connect a seat source on Cost sources to see spend by
            developer.
          </p>
        </div>
      ) : (
        <section className="detail-section">
          <div className="inference-body">
            <div className="inference-col">
              <span className="chart-title">Trend</span>
              <TrendChart trend={data.build_trend} />
            </div>
            <div className="inference-col">
              <span className="chart-title">By developer · {money(data.build_total)} total</span>
              <SpendBars
                rows={data.build_by_developer.map((d) => ({
                  label: d.label,
                  amount: d.amount,
                  pct: d.pct,
                  models: d.by_tool.map((t) => ({
                    label: prettyTool(t.tool),
                    amount: t.amount,
                    pct: t.pct,
                  })),
                }))}
              />
            </div>
          </div>
        </section>
      )}

      {data && data.developer_activity.length === 0 && (
        <section className="detail-section">
          <h3 className="breakdown-subhead">Engineering activity</h3>
          <ActivityGap
            coverage={data.activity_coverage}
            start={data.start}
            end={data.end}
            onDiscovered={() => setReload((n) => n + 1)}
          />
        </section>
      )}

      {data && data.developer_activity.length > 0 && (
        <section className="detail-section">
          <h3 className="breakdown-subhead">Engineering activity</h3>
          <p className="section-sub muted">
            What each developer shipped over the same period, from the merged-PR evidence behind
            every build-cost attribution. This is <strong>activity, not performance</strong> — it
            counts what was shipped, not how hard or how valuable it was, and a large PR is not a
            better one. Read it next to the spend above, never as a ranking of people.
          </p>
          <table className="features-table">
            <thead>
              <tr>
                <th>Developer</th>
                <th className="num">PRs</th>
                <th className="num">Features</th>
                <th className="num">Commits</th>
                <th className="num">Files</th>
                <th className="num">Lines</th>
                <th className="num">Build cost</th>
                <th className="num">Cost / PR</th>
              </tr>
            </thead>
            <tbody>
              {data.developer_activity.map((d) => (
                <tr key={d.handle}>
                  <td>{d.label}</td>
                  <td className="num">{num(d.prs)}</td>
                  <td className="num" title="Distinct features their merged PRs touched">
                    {num(d.features)}
                  </td>
                  <td className="num">{num(d.commits)}</td>
                  <td className="num">{num(d.files_changed)}</td>
                  <td className="num">
                    <LineCounts added={d.additions} removed={d.deletions} />
                  </td>
                  <td className="num">{money(d.build_cost)}</td>
                  <td className="num" title="AI coding-tool spend divided by PRs merged">
                    {unitMoney(d.cost_per_pr)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
    </>
  );
}

/** "4 Apr 2026" — a date the reader can line up against a calendar. */
function shortDay(iso: string): string {
  return new Date(iso.length > 10 ? iso : `${iso}T00:00:00Z`).toLocaleDateString("en-US", {
    day: "numeric",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  });
}

/** "April 2026" / "April – May 2026" — the window, in words. */
function windowLabel(start: string, end: string): string {
  const fmt = (iso: string) =>
    new Date(`${iso}T00:00:00Z`).toLocaleDateString("en-US", {
      month: "long",
      year: "numeric",
      timeZone: "UTC",
    });
  return start.slice(0, 7) === end.slice(0, 7) ? fmt(start) : `${fmt(start)} – ${fmt(end)}`;
}

/**
 * Why the activity table is empty — and what, if anything, to do about it.
 *
 * The table is built from merged-PR evidence that GitHub discovery collects, so
 * "no rows" has several quite different causes and only one of them is the
 * reader's to fix. Rendering nothing at all, which is what this used to do, left
 * a connected customer wondering whether their developers had shipped nothing.
 */
function ActivityGap({
  coverage,
  start,
  end,
  onDiscovered,
}: {
  coverage: ProviderSpend["activity_coverage"];
  start: string;
  end: string;
  onDiscovered: () => void;
}) {
  const window = windowLabel(start, end);

  if (!coverage.github_connected && coverage.runs === 0 && coverage.dated_prs === 0) {
    return (
      <div className="empty-state">
        <p className="empty-title">No pull-request evidence yet</p>
        <p className="muted">
          This table is built from the merged PRs behind each build-cost attribution. Connect GitHub
          on <Link to="/cost-sources">Cost sources</Link>, then run discovery on{" "}
          <Link to="/features">Features</Link>.
        </p>
      </div>
    );
  }

  if (coverage.runs === 0 && coverage.dated_prs === 0) {
    return (
      <div className="empty-state">
        <p className="empty-title">Discovery has not run yet</p>
        <p className="muted">
          GitHub is connected, but no merged pull requests have been collected yet.
        </p>
        <RunDiscoveryButton start={start} window={window} onDone={onDiscovered} />
        {coverage.last_run_status === "error" && (
          <p className="muted activity-gap-note">
            The last attempt failed
            {coverage.last_run_at ? ` on ${shortDay(coverage.last_run_at)}` : ""}. Check the GitHub
            connection on <Link to="/cost-sources">Cost sources</Link>.
          </p>
        )}
      </div>
    );
  }

  // Evidence exists, but none of it lands in this window. Say what discovery has
  // actually covered — recorded from the runs themselves, so this can tell "we
  // looked and there was nothing" from "we never looked".
  const coveredByRuns =
    coverage.covered_from && coverage.covered_to
      ? `${shortDay(coverage.covered_from)} – ${shortDay(coverage.covered_to)}`
      : null;
  const looked = coverage.covered_from !== null && start >= coverage.covered_from;

  return (
    <div className="empty-state">
      <p className="empty-title">No pull requests merged in {window}</p>
      <p className="muted">
        {coveredByRuns && looked
          ? `Discovery has covered ${coveredByRuns}, which includes ${window} — so this window really was quiet.`
          : coveredByRuns
            ? `Discovery has only reached back to ${shortDay(coverage.covered_from!)}, so ${window} has never been collected.`
            : evidenceSentence(coverage)}
      </p>
      {!looked && <RunDiscoveryButton start={start} window={window} onDone={onDiscovered} />}
      {coverage.last_run_at && (
        <p className="muted activity-gap-note">
          Last run {shortDay(coverage.last_run_at)}
          {coverage.last_run_trigger === "scheduled" ? " (automatic)" : ""}
          {coverage.last_run_status === "error" ? " — it failed" : ""}.
        </p>
      )}
      {coverage.undated_prs > 0 && (
        <p className="muted activity-gap-note">
          {coverage.undated_prs} pull request{coverage.undated_prs === 1 ? "" : "s"} discovered
          before merge dates were recorded cannot be placed in any window, and are not counted here.
          Re-running discovery will date them.
        </p>
      )}
    </div>
  );
}

/** The fallback for tenants whose PR evidence predates run recording: all we can
 *  say is which merge dates we hold, not which windows were looked at. */
function evidenceSentence(coverage: ProviderSpend["activity_coverage"]): string {
  if (coverage.first_merged && coverage.last_merged) {
    return (
      `Meter holds PR evidence for ${shortDay(coverage.first_merged)} – ` +
      `${shortDay(coverage.last_merged)}, but no record of which windows discovery has looked at.`
    );
  }
  return "Meter holds PR evidence, but none of it falls in this window.";
}

/**
 * Runs discovery over exactly the window being viewed.
 *
 * The window travels as a date rather than as a lookback: the server owns the
 * clock, and a browser converting months into "days ago" would be one timezone
 * away from being wrong.
 */
function RunDiscoveryButton({
  start,
  window: label,
  onDone,
}: {
  start: string;
  window: string;
  onDone: () => void;
}) {
  const [scope, setScope] = useState<DiscoveryScope | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);

  useEffect(() => {
    api
      .discoveryScope()
      .then(setScope)
      .catch(() => setScope(null));
  }, []);

  // Without a saved owner there is nothing to run against, so the wizard is the
  // honest destination rather than a button that cannot work.
  if (!scope?.owner) {
    return (
      <p className="muted">
        Run discovery on <Link to="/features">Features</Link> to collect them.
      </p>
    );
  }

  async function run() {
    setBusy(true);
    setError(null);
    setDone(null);
    try {
      const summary = await api.runDiscovery(scope!.owner!, scope!.repos, 90, start);
      setDone(
        `Collected ${summary.prs} pull request${summary.prs === 1 ? "" : "s"} across ${
          summary.repos_with_prs.length
        } repositor${summary.repos_with_prs.length === 1 ? "y" : "ies"}.`,
      );
      onDone();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Discovery could not be run.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="activity-gap-action">
      <button type="button" onClick={run} disabled={busy}>
        {busy ? "Running discovery…" : `Run discovery back to ${label.split(" – ")[0]}`}
      </button>
      <span className="muted settings-hint">
        Reads merged pull requests from {scope.owner}
        {scope.repos.length > 0 ? ` (${scope.repos.length} repositories)` : ""} and may raise new
        feature proposals for review.
      </span>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {done && (
        <p className="muted" role="status">
          {done}
        </p>
      )}
    </div>
  );
}

/** Lines added / removed. Unknown for PRs discovered before line counts were
 *  recorded — shown as an em dash rather than as a misleading zero. */
function LineCounts({ added, removed }: { added: number | null; removed: number | null }) {
  if (added === null && removed === null) return <>—</>;
  return (
    <span title={`${num(added)} added, ${num(removed)} removed`}>
      +{compact(added)} <span className="muted">/</span> −{compact(removed)}
    </span>
  );
}
