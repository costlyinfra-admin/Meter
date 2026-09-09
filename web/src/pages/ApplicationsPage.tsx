/**
 * Applications — the product surface above features, and the economics of the
 * workflows each one runs.
 *
 * The columns are chosen to answer a buyer's question rather than an operator's:
 * what does this application cost, what does one run of it cost, and is that
 * moving. Live and stale counts sit beside the money because an agent that is
 * stuck is a cost problem, not only an availability one.
 */
import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, ApiError, type AiApplication } from "../api";
import { money, num } from "../format";

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

export function ApplicationsPage() {
  const [apps, setApps] = useState<AiApplication[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .aiApplications()
      .then((r) => setApps(r.applications))
      .catch((err) =>
        setError(err instanceof ApiError ? err.message : "Could not load applications."),
      );
  }, []);

  return (
    <div className="content">
      <div className="dash-head">
        <div>
          <h1>Applications</h1>
          <p className="muted dash-sub">
            AI applications and the economics of the workflows they run.
          </p>
        </div>
      </div>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      {!apps ? (
        <p className="muted">Loading applications…</p>
      ) : apps.length === 0 ? (
        <div className="trace-empty">
          <h2>No applications yet</h2>
          <p className="muted">
            An application appears the first time an instrumented workflow reports a run.
          </p>
          <Link className="button-link" to="/install-sdk">
            Install SDK
          </Link>
        </div>
      ) : (
        <div className="table-scroll">
          <table className="data-table">
            <caption className="sr-only">AI applications and their economics</caption>
            <thead>
              <tr>
                <th scope="col">Application</th>
                <th scope="col">Features</th>
                <th scope="col">AI spend</th>
                <th scope="col">Runs</th>
                <th scope="col">Active</th>
                <th scope="col">Stale</th>
                <th scope="col">Cost per run</th>
                <th scope="col">Tokens</th>
                <th scope="col">Error rate</th>
                <th scope="col">Change</th>
              </tr>
            </thead>
            <tbody>
              {apps.map((a) => (
                <tr key={a.id}>
                  <td>
                    <Link to={`/applications/${a.id}`}>{a.name}</Link>
                  </td>
                  <td className="numeric">{num(a.features)}</td>
                  <td className="numeric">{money(a.spend)}</td>
                  <td className="numeric">{num(a.runs)}</td>
                  <td className="numeric">{a.active > 0 ? num(a.active) : "—"}</td>
                  <td className="numeric">
                    {a.stale > 0 ? (
                      <span className="badge trace-status-stale">{a.stale}</span>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td className="numeric">
                    {a.cost_per_run === null ? "—" : money(a.cost_per_run)}
                  </td>
                  <td className="numeric">{num(a.tokens)}</td>
                  <td className="numeric">
                    {a.error_rate === null ? "—" : `${(a.error_rate * 100).toFixed(1)}%`}
                  </td>
                  <td className="numeric">
                    <Change value={a.spend_change} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export function ApplicationDetail() {
  const { id = "" } = useParams();
  const [app, setApp] = useState<AiApplication | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    api
      .aiApplication(id)
      .then(setApp)
      .catch((err) =>
        setError(err instanceof ApiError ? err.message : "Could not load this application."),
      );
  }, [id]);

  useEffect(load, [load]);

  if (error) {
    return (
      <div className="content">
        <p className="error" role="alert">
          {error}
        </p>
        <Link className="link" to="/applications">
          Back to applications
        </Link>
      </div>
    );
  }
  if (!app)
    return (
      <div className="content">
        <p className="muted">Loading…</p>
      </div>
    );

  return (
    <div className="content">
      <div className="dash-head">
        <div>
          <Link className="link trace-back" to="/applications">
            ← Applications
          </Link>
          <h1>{app.name}</h1>
          <p className="muted dash-sub">
            {app.description ?? "AI application"}
            {app.owner && ` · ${app.owner}`} · {app.slug}
          </p>
        </div>
      </div>

      <div className="kpi-row">
        <div className="kpi-card">
          <span className="kpi-label">AI spend</span>
          <span className="kpi-value">{money(app.spend)}</span>
        </div>
        <div className="kpi-card">
          <span className="kpi-label">Cost per run</span>
          <span className="kpi-value">
            {app.cost_per_run === null ? "—" : money(app.cost_per_run)}
          </span>
        </div>
        <div className="kpi-card">
          <span className="kpi-label">Total tokens</span>
          <span className="kpi-value">{num(app.tokens)}</span>
        </div>
        <div className="kpi-card">
          <span className="kpi-label">Runs</span>
          <span className="kpi-value">{num(app.runs)}</span>
        </div>
      </div>

      <p className="muted app-secondary">
        {num(app.active)} running · {num(app.stale)} stale ·{" "}
        {app.error_rate === null ? "no runs" : `${(app.error_rate * 100).toFixed(1)}% errors`} ·{" "}
        <Link to={`/traces?tab=all`}>view traces</Link>
      </p>

      <div className="app-breakdowns">
        <section>
          <h2>By feature</h2>
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">Feature</th>
                <th scope="col">Spend</th>
                <th scope="col">Runs</th>
              </tr>
            </thead>
            <tbody>
              {(app.by_feature ?? []).map((f) => (
                <tr key={f.feature}>
                  <td>{f.feature}</td>
                  <td className="numeric">{money(f.spend)}</td>
                  <td className="numeric">{num(f.runs)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>

        <section>
          <h2>By model</h2>
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">Model</th>
                <th scope="col">Spend</th>
                <th scope="col">Calls</th>
              </tr>
            </thead>
            <tbody>
              {(app.by_model ?? []).map((m) => (
                <tr key={`${m.provider}-${m.model}`}>
                  <td>{m.model}</td>
                  <td className="numeric">{money(m.spend)}</td>
                  <td className="numeric">{num(m.calls)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>

        {(app.releases ?? []).length > 0 && (
          <section>
            <h2>Recent releases</h2>
            <table className="data-table">
              <thead>
                <tr>
                  <th scope="col">Release</th>
                  <th scope="col">Runs</th>
                  <th scope="col">Spend</th>
                </tr>
              </thead>
              <tbody>
                {(app.releases ?? []).map((r) => (
                  <tr key={r.release}>
                    <td>{r.release}</td>
                    <td className="numeric">{num(r.runs)}</td>
                    <td className="numeric">{money(r.spend)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        )}
      </div>
    </div>
  );
}
