/**
 * Reconciliation → Connected providers.
 *
 * The primary reconciliation path: what each connected provider billed, against
 * what the metering SDK measured, for one month. No upload — both numbers are
 * already synced.
 *
 * Three things this screen must not do.
 *
 * It must not call a missing measurement a variance. A customer who has not
 * installed the SDK has a bill and nothing to compare it with; rendering that
 * as "100% variance" on every provider would be alarming and wrong, so
 * `no_metered_data` is its own state with its own remedy.
 *
 * It must not show a variance on a dimension only one side records. Meter's
 * metered rows carry no day and no workspace, so the per-day and per-account
 * figures are the provider's alone and are labelled as such — a blank column
 * beside them would read as zero rather than as absent.
 *
 * And it must not imply it changed anything. Reconciliation reads; it never
 * writes to tracked cost.
 */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  api,
  ApiError,
  type BillingBreakdown,
  type BillingComparison as Comparison,
  type BillingProvider,
  type BillingStatus,
} from "../api";

const STATUS_LABEL: Record<BillingStatus, string> = {
  matched: "Matched",
  variance: "Variance found",
  no_metered_data: "No metered data",
  billing_access_required: "Billing access required",
  not_supported: "Not supported",
};

/** What each state means, in the words someone would use to act on it. */
const STATUS_HINT: Record<BillingStatus, string> = {
  matched: "The bill and the metered spend agree for this period.",
  variance: "The bill and the metered spend disagree by more than the tolerance.",
  no_metered_data:
    "Billing data is here, but nothing was metered to compare it against — install the metering SDK for this provider.",
  billing_access_required:
    "Meter cannot read this provider's billing data for the period. Connect it to reconcile.",
  not_supported:
    "Meter has spend for this provider that no billing API can confirm — a self-hosted model, or a provider outside the connector list. There is nothing to reconcile it against.",
};

const money = (n: number, currency = "USD") =>
  n.toLocaleString(undefined, { style: "currency", currency, maximumFractionDigits: 2 });

function freshness(iso: string | null): string {
  if (!iso) return "—";
  const hours = Math.round((Date.now() - new Date(iso).getTime()) / 3_600_000);
  if (hours < 1) return "just now";
  if (hours < 24) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

export function BillingComparison() {
  const [data, setData] = useState<Comparison | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    api
      .billingComparison()
      .then((next) => live && setData(next))
      .catch(
        (err) =>
          live && setError(err instanceof ApiError ? err.message : "Could not load billing data."),
      );
    return () => {
      live = false;
    };
  }, []);

  if (error)
    return (
      <p className="error" role="alert">
        {error}
      </p>
    );
  if (!data) return <p className="muted">Loading…</p>;

  if (data.providers.length === 0) {
    return (
      <div className="source-section recon-empty">
        <h2>No billing data available yet</h2>
        <p className="muted">
          Connect a provider on Connect sources and Meter will read its billing data, then compare
          it with what the metering SDK measured.
        </p>
        <Link className="button-link" to="/cost-sources">
          Connect billing data
        </Link>
        <p className="muted recon-fallback">
          For a provider Meter cannot read, a closed period, or a final invoice, you can{" "}
          <Link to="/reconciliation/import">import a statement</Link> instead.
        </p>
      </div>
    );
  }

  return (
    <section className="source-section">
      <p className="muted recon-period">
        Billing period <strong>{data.period.slice(0, 7)}</strong>. Agreement within{" "}
        {money(data.tolerance.absolute)} or {data.tolerance.percent}% counts as matched.
      </p>
      <div className="kb-table-wrap">
        <table className="features-table recon-table">
          <thead>
            <tr>
              <th scope="col">Provider</th>
              <th scope="col">Status</th>
              <th scope="col">Meter tracked</th>
              <th scope="col">Provider reported</th>
              <th scope="col">Variance</th>
              <th scope="col">Billing data</th>
              <th scope="col" aria-label="Actions" />
            </tr>
          </thead>
          <tbody>
            {data.providers.map((p) => (
              <ProviderRow
                key={p.provider}
                provider={p}
                period={data.period}
                open={open === p.provider}
                onToggle={() => setOpen(open === p.provider ? null : p.provider)}
              />
            ))}
          </tbody>
        </table>
      </div>
      <p className="muted recon-fallback">
        Reconciliation reads your data and never changes it. For a provider Meter cannot read, a
        closed period, or a final invoice,{" "}
        <Link to="/reconciliation/import">import a statement</Link> instead.
      </p>
    </section>
  );
}

function ProviderRow({
  provider,
  period,
  open,
  onToggle,
}: {
  provider: BillingProvider;
  period: string;
  open: boolean;
  onToggle: () => void;
}) {
  const showsVariance = provider.status === "variance";
  return (
    <>
      <tr>
        <td>
          {provider.name}
          {provider.estimated && (
            <span className="recon-flag" title="The provider has not closed this month yet">
              {" "}
              estimated
            </span>
          )}
        </td>
        <td>
          <span className={`recon-status recon-status-${provider.status}`}>
            {STATUS_LABEL[provider.status]}
          </span>
        </td>
        <td>{provider.tracked > 0 ? money(provider.tracked, provider.currency) : "—"}</td>
        <td>
          {provider.provider_reported > 0
            ? money(provider.provider_reported, provider.currency)
            : "—"}
        </td>
        <td>
          {showsVariance ? (
            <>
              {money(provider.variance, provider.currency)}
              {provider.variance_pct !== null && (
                <span className="muted"> ({provider.variance_pct.toFixed(1)}%)</span>
              )}
            </>
          ) : (
            "—"
          )}
        </td>
        <td>{freshness(provider.billing_updated_at)}</td>
        <td>
          {provider.status === "billing_access_required" ? (
            <Link className="button-link" to="/cost-sources">
              Connect billing data
            </Link>
          ) : showsVariance ? (
            <button className="secondary" onClick={onToggle} aria-expanded={open}>
              {open ? "Hide breakdown" : "Show breakdown"}
            </button>
          ) : null}
        </td>
      </tr>
      <tr className="recon-hint-row">
        <td colSpan={7}>
          <span className="muted">{STATUS_HINT[provider.status]}</span>
          {provider.mixed_currency && (
            <span className="muted">
              {" "}
              This provider billed in more than one currency; totals across them are not comparable.
            </span>
          )}
        </td>
      </tr>
      {open && (
        <tr>
          <td colSpan={7}>
            <Breakdown provider={provider.provider} period={period} currency={provider.currency} />
          </td>
        </tr>
      )}
    </>
  );
}

function Breakdown({
  provider,
  period,
  currency,
}: {
  provider: string;
  period: string;
  currency: string;
}) {
  const [data, setData] = useState<BillingBreakdown | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    api
      .billingBreakdown(provider, period.slice(0, 7))
      .then((next) => live && setData(next))
      .catch(() => live && setError("Could not load the breakdown."));
    return () => {
      live = false;
    };
  }, [provider, period]);

  if (error) return <p className="error">{error}</p>;
  if (!data) return <p className="muted">Loading…</p>;

  return (
    <div className="recon-breakdown">
      <h4>By model</h4>
      <p className="muted">The one dimension both the bill and the metered spend record.</p>
      <table className="features-table">
        <thead>
          <tr>
            <th scope="col">Model</th>
            <th scope="col">Meter tracked</th>
            <th scope="col">Provider reported</th>
            <th scope="col">Variance</th>
          </tr>
        </thead>
        <tbody>
          {data.by_model.map((m) => (
            <tr key={m.model}>
              <td>{m.model}</td>
              <td>{money(m.tracked, currency)}</td>
              <td>{money(m.provider_reported, currency)}</td>
              <td>
                {money(m.variance, currency)}
                {m.variance_pct !== null && (
                  <span className="muted"> ({m.variance_pct.toFixed(1)}%)</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <h4>Provider-reported only</h4>
      <p className="muted">
        The bill carries a day and an account; Meter&rsquo;s metered rows carry neither, so these
        cannot be compared — they are here to show where and when the provider&rsquo;s spend fell.
      </p>
      <div className="recon-provider-only">
        <div>
          <h5>By account</h5>
          <ul className="kb-list">
            {data.provider_only.by_account.map((a) => (
              <li key={a.account}>
                {a.account} — {money(a.provider_reported, currency)}
              </li>
            ))}
            {data.provider_only.by_account.length === 0 && <li className="muted">Not reported.</li>}
          </ul>
        </div>
        <div>
          <h5>By day</h5>
          <ul className="kb-list">
            {data.provider_only.by_day.map((d) => (
              <li key={d.day}>
                {d.day} — {money(d.provider_reported, currency)}
              </li>
            ))}
            {data.provider_only.by_day.length === 0 && <li className="muted">Not reported.</li>}
          </ul>
        </div>
      </div>
    </div>
  );
}
