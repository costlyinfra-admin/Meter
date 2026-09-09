/**
 * Applications — the product surface above features, and the economics of the
 * workflows each one runs.
 *
 * Deliberately built from the same parts as the feature views, because it
 * answers the same question one level up. The list is the Overview's by-feature
 * table (`features-table`, click a row to drill in, search to narrow); the
 * detail is the feature drill-down (`detail-section` with a section head, a
 * `mini-table`, and a legend saying what the numbers are). Someone who has
 * learned one has learned both.
 *
 * The columns answer a buyer's question rather than an operator's: what does
 * this application cost, what does one run of it cost, and is that moving. Live
 * and stale counts sit beside the money because an agent that is stuck is a
 * cost problem, not only an availability one.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api, ApiError, type AiApplication } from "../api";
import { compact, money, num } from "../format";
import { TRACE_WINDOWS, daysFromParams } from "./traceWindow";

/** A change against the previous comparable window. `null` means there was
 *  nothing to compare against, which is not the same as "no change". */
function Change({ value }: { value: number | null }) {
  if (value === null || !Number.isFinite(value)) return <span className="muted">—</span>;
  const up = value > 0;
  return (
    <span className={`delta ${up ? "delta-up" : "delta-down"}`}>
      {up ? "▲" : "▼"} {Math.abs(value * 100).toFixed(0)}%
    </span>
  );
}

/** Live/stale as a badge pair, the way the feature table ends in a health badge.
 *  A run that has gone quiet is called out; one that simply finished is not. */
function RunState({ active, stale }: { active: number; stale: number }) {
  if (stale > 0)
    return (
      <span className="badge trace-status-stale" title="Runs that have gone quiet">
        {num(stale)} stale
      </span>
    );
  if (active > 0)
    return (
      <span className="badge trace-status-running" title="Runs in flight right now">
        {num(active)} running
      </span>
    );
  return <span className="muted">idle</span>;
}

function WindowSelector({ days, onChange }: { days: number; onChange: (d: number) => void }) {
  return (
    <div className="period-controls detail-period">
      <span className="muted period-label">Showing</span>
      <select
        className="period-select"
        aria-label="Time window"
        value={days}
        onChange={(e) => onChange(Number(e.target.value))}
      >
        {TRACE_WINDOWS.map((w) => (
          <option key={w.days} value={w.days}>
            {w.label}
          </option>
        ))}
      </select>
    </div>
  );
}

export function ApplicationsPage() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const days = daysFromParams(searchParams);
  const [apps, setApps] = useState<AiApplication[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");

  useEffect(() => {
    let live = true;
    setApps(null);
    api
      .aiApplications({ days })
      .then((r) => live && setApps(r.applications))
      .catch(
        (err) =>
          live && setError(err instanceof ApiError ? err.message : "Could not load applications."),
      );
    return () => {
      live = false;
    };
  }, [days]);

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return apps ?? [];
    return (apps ?? []).filter(
      (a) => a.name.toLowerCase().includes(q) || a.slug.toLowerCase().includes(q),
    );
  }, [apps, query]);

  return (
    <div className="content">
      <div className="dash-head">
        <div>
          <h1>Applications</h1>
          <p className="muted dash-sub">
            AI applications and the economics of the workflows they run.
          </p>
        </div>
        <WindowSelector
          days={days}
          onChange={(d) => setSearchParams({ days: String(d) }, { replace: true })}
        />
      </div>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      {apps && apps.length > 0 && (
        <div className="tabs" role="presentation">
          <input
            type="search"
            className="tab-search"
            value={query}
            placeholder="Search applications…"
            aria-label="Search applications"
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
      )}

      {!apps && !error ? (
        <p className="muted">Loading applications…</p>
      ) : apps && apps.length === 0 ? (
        <div className="empty-state">
          <p className="empty-title">No applications yet</p>
          <p className="muted">
            An application appears the first time an instrumented workflow reports a run.
          </p>
          <Link className="button-link" to="/install-sdk">
            Install SDK
          </Link>
        </div>
      ) : apps && visible.length === 0 ? (
        <div className="empty-state">
          <p className="empty-title">No applications match “{query}”</p>
          <p className="muted">Clear the search to see all {num(apps.length)}.</p>
        </div>
      ) : apps ? (
        <>
          <table className="features-table">
            <caption className="sr-only">AI applications and their economics</caption>
            <thead>
              <tr>
                <th>Application</th>
                <th className="num" title="Features this application's runs attribute to">
                  Features
                </th>
                <th className="num">AI spend</th>
                <th className="num">Runs</th>
                <th className="num" title="Average cost of one finished run">
                  Cost / run
                </th>
                <th className="num">Tokens</th>
                <th className="num" title="Share of runs that ended in error">
                  Error rate
                </th>
                <th className="num" title="Spend against the previous comparable window">
                  Change
                </th>
                <th title="Runs in flight, and runs that have gone quiet">Live</th>
              </tr>
            </thead>
            <tbody>
              {visible.map((a) => (
                <tr
                  key={a.id}
                  className="feature-row"
                  onClick={() => navigate(`/applications/${a.id}?days=${days}`)}
                >
                  <td>
                    <Link
                      to={`/applications/${a.id}?days=${days}`}
                      onClick={(e) => e.stopPropagation()}
                    >
                      {a.name}
                    </Link>
                  </td>
                  <td className="num">{num(a.features)}</td>
                  <td className="num">{money(a.spend)}</td>
                  <td className="num">{num(a.runs)}</td>
                  <td className="num">{a.cost_per_run === null ? "—" : money(a.cost_per_run)}</td>
                  <td className="num">{compact(a.tokens)}</td>
                  <td className="num">
                    {a.error_rate === null ? "—" : `${(a.error_rate * 100).toFixed(1)}%`}
                  </td>
                  <td className="num">
                    <Change value={a.spend_change} />
                  </td>
                  <td>
                    <RunState active={a.active} stale={a.stale} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="muted legend">
            Cost per run counts finished runs only — a run still in flight has only part of its cost
            recorded. “Live” is derived at read time from each run's last activity, so a stale run
            has not failed and may still finish.
          </p>
        </>
      ) : null}
    </div>
  );
}

/** A row's share of a total, for the breakdown tables. */
function share(part: number, total: number): string {
  if (!total) return "—";
  return `${((part / total) * 100).toFixed(1)}%`;
}

export function ApplicationDetail() {
  const { id = "" } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const days = daysFromParams(searchParams);
  const [app, setApp] = useState<AiApplication | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    setApp(null);
    setError(null);
    api
      .aiApplication(id, days)
      .then(setApp)
      .catch((err) =>
        setError(err instanceof ApiError ? err.message : "Could not load this application."),
      );
  }, [id, days]);

  useEffect(load, [load]);

  const byFeature = app?.by_feature ?? [];
  const byModel = app?.by_model ?? [];
  const releases = app?.releases ?? [];
  const modelTotal = byModel.reduce((sum, m) => sum + m.spend, 0);

  return (
    <div className="content">
      <Link to="/applications" className="link breadcrumb">
        ← All applications
      </Link>

      {/* Scopes every number on this page, mirroring the feature drill-down's
          review period and preserved in the URL so a link is shareable. */}
      <WindowSelector
        days={days}
        onChange={(d) => setSearchParams({ days: String(d) }, { replace: true })}
      />

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {app === null && !error ? (
        <p className="muted">Loading…</p>
      ) : app ? (
        <div>
          <h1>{app.name}</h1>
          {app.description && <p className="muted">{app.description}</p>}
          <p className="detail-meta">
            {/* <code>, not a .badge: badges capitalize, and this is the literal
                value that goes into METER_APPLICATION. "Document-Review" is not
                the slug and would be copied wrong. */}
            <code className="slug-tag">{app.slug}</code>
            <RunState active={app.active} stale={app.stale} />
            {app.owner && <span className="muted">owned by {app.owner}</span>}
            <span className="muted">
              {num(app.features)} feature{app.features === 1 ? "" : "s"}
            </span>
            {app.error_rate !== null && (
              <span className="muted" title="Share of runs in this window that ended in error">
                {(app.error_rate * 100).toFixed(1)}% errors
              </span>
            )}
            {/* Traces now sends application_id, so this really does filter —
                and it carries the window, so the run list covers the same
                period as the numbers above it. */}
            <Link to={`/traces?application_id=${app.id}&days=${days}`}>View traces</Link>
          </p>

          {/* ---- Cost by feature ---- */}
          <section className="detail-section">
            <div className="section-head">
              <div>
                <h2>Cost by feature</h2>
                <span className="section-sub muted">
                  What this application's runs cost, and which features they were for.
                </span>
              </div>
              <div className="section-stats">
                <span>
                  <strong>{money(app.spend)}</strong> in period
                </span>
                <span>
                  <strong>{app.cost_per_run === null ? "—" : money(app.cost_per_run)}</strong> per
                  run
                </span>
                <span>
                  <strong>{num(app.runs)}</strong> run{app.runs === 1 ? "" : "s"}
                </span>
                <span>
                  <strong>{compact(app.tokens)}</strong> tokens
                </span>
              </div>
            </div>
            {byFeature.length === 0 ? (
              <p className="muted">No attributed runs in this period.</p>
            ) : (
              <>
                <table className="mini-table">
                  <thead>
                    <tr>
                      <th>Feature</th>
                      <th className="num">Spend</th>
                      <th className="num">Runs</th>
                      <th className="num">Cost / run</th>
                      <th className="num">Share</th>
                    </tr>
                  </thead>
                  <tbody>
                    {byFeature.map((f) => (
                      <tr key={f.feature}>
                        <td>{f.feature}</td>
                        <td className="num">{money(f.spend)}</td>
                        <td className="num">{num(f.runs)}</td>
                        <td className="num">{f.runs ? money(f.spend / f.runs) : "—"}</td>
                        <td className="num">{share(f.spend, app.spend)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <p className="muted legend">
                  Runs whose feature was never set, or whose feature has since been deleted, are
                  counted here as Unattributed rather than dropped.
                </p>
              </>
            )}
          </section>

          {/* ---- Cost by model ---- */}
          <section className="detail-section">
            <div className="section-head">
              <div>
                <h2>Cost by model</h2>
                <span className="section-sub muted">
                  {money(modelTotal)} in period · one run can call several models
                </span>
              </div>
            </div>
            {byModel.length === 0 ? (
              <p className="muted">No model calls in this period.</p>
            ) : (
              <table className="mini-table">
                <thead>
                  <tr>
                    <th>Model</th>
                    <th>Provider</th>
                    <th className="num">Spend</th>
                    <th className="num">Calls</th>
                    <th className="num">Share</th>
                  </tr>
                </thead>
                <tbody>
                  {byModel.map((m) => (
                    <tr key={`${m.provider}-${m.model}`}>
                      <td>{m.model}</td>
                      <td className="muted">{m.provider}</td>
                      <td className="num">{money(m.spend)}</td>
                      <td className="num">{num(m.calls)}</td>
                      <td className="num">{share(m.spend, modelTotal)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>

          {/* ---- Releases ---- */}
          {releases.length > 0 && (
            <section className="detail-section">
              <div className="section-head">
                <div>
                  <h2>Recent releases</h2>
                  <span className="section-sub muted">
                    Cost per run by release — how a deploy moved the price of the work.
                  </span>
                </div>
              </div>
              <table className="mini-table">
                <thead>
                  <tr>
                    <th>Release</th>
                    <th className="num">Runs</th>
                    <th className="num">Spend</th>
                    <th className="num">Cost / run</th>
                  </tr>
                </thead>
                <tbody>
                  {releases.map((r) => (
                    <tr key={r.release}>
                      <td>{r.release}</td>
                      <td className="num">{num(r.runs)}</td>
                      <td className="num">{money(r.spend)}</td>
                      <td className="num">{r.runs ? money(r.spend / r.runs) : "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="muted legend">
                Releases are the values your SDK reported as <code>METER_RELEASE_VERSION</code>;
                runs that reported none are not listed.
              </p>
            </section>
          )}
        </div>
      ) : null}
    </div>
  );
}
