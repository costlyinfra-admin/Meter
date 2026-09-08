/**
 * The Infrastructure cost tab: cloud providers, and what their bills came to.
 *
 * A cloud bill is not a model bill. There is no api key and no token count —
 * just services, accounts, regions and tags — so this reuses the connector card
 * (state, Sync now, setup guide, errors) but gives it its own detail panel:
 * spend by service, how much of it reached a feature, and what was deliberately
 * left out.
 *
 * "Deliberately left out" is the part worth reading. Amazon Bedrock's dollars
 * already arrive through the Bedrock connector on the Inference tab. They are
 * recorded here and then excluded from every infrastructure total, and the
 * panel says so with the number, because a customer comparing this page against
 * their AWS console should be able to see exactly where the difference went.
 *
 * Providers come from the backend registry rather than a list in this file, so
 * Azure and GCP appear as "coming soon" today and become connectable when their
 * ingestion lands — no change here.
 */
import { useCallback, useEffect, useState } from "react";
import { api, ApiError, type InfraProvider, type InfraSummary } from "../api";
import { money } from "../format";
import { ConnectorMark } from "./ConnectorMark";
import { ConnectorRow } from "./ConnectorRow";

/** How a category reads in the panel. Matches backend/meter/infra_classify.py. */
const CATEGORY_LABELS: Record<string, string> = {
  infrastructure: "Infrastructure",
  self_hosted: "Self-hosted models",
  build: "Build",
  inference: "Inference",
  unclassified: "Unclassified",
};

function syncedAt(iso: string | null | undefined): string {
  if (!iso) return "never";
  const when = new Date(iso);
  return Number.isNaN(when.getTime())
    ? "never"
    : when.toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
      });
}

/** The one-line status under a provider's name. */
function statusLine(p: InfraProvider): string {
  if (!p.connected) return "Cloud infrastructure";
  if (!p.last_sync) return "Connected — not synced yet";
  if (p.last_sync.status === "error")
    return `Last sync failed — ${syncedAt(p.last_sync.started_at)}`;
  return `Last synced ${syncedAt(p.last_sync.started_at)}`;
}

/** A provider we list but cannot connect yet. Shown, deliberately not offered. */
function ComingSoonRow({ provider }: { provider: InfraProvider }) {
  return (
    <li className="connector-row">
      <div className="connector-head">
        <span className="connector-info">
          <ConnectorMark type={provider.type} name={provider.name} />
          <span className="connector-text">
            <span className="connector-name">{provider.name}</span>
            <span className="connector-category">{provider.note}</span>
          </span>
        </span>
        <span className="badge-soon">Coming soon</span>
      </div>
    </li>
  );
}

/** What a connected provider's last sync actually imported. */
function InfraDetail({ provider, refreshKey }: { provider: InfraProvider; refreshKey: number }) {
  const [summary, setSummary] = useState<InfraSummary | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    api
      .infraSummary(provider.type)
      .then((s) => live && setSummary(s))
      .catch((err) => live && setError(err instanceof ApiError ? err.message : "Could not load."));
    return () => {
      live = false;
    };
  }, [provider.type, refreshKey]);

  const config = provider.config;
  return (
    <div className="infra-detail">
      {config && (
        <p className="muted infra-config">
          Attributing by the <code>{config.tag}</code> tag · {config.metric} ·{" "}
          {config.granularity.toLowerCase()} · {config.region}
        </p>
      )}
      {provider.last_sync?.status === "error" && provider.last_sync.error_message && (
        <p className="error" role="alert">
          {provider.last_sync.error_message}
        </p>
      )}
      {error && <p className="error">{error}</p>}
      {summary && (
        <>
          <p className="muted">
            {money(summary.total)} of infrastructure this month across {summary.rows} line{" "}
            {summary.rows === 1 ? "item" : "items"} — {money(summary.attributed)} attributed to
            features, {money(summary.unattributed)} unattributed.
          </p>
          {summary.excluded > 0 && (
            <p className="muted infra-excluded">
              {money(summary.excluded)} of Amazon Bedrock spend was recorded but not counted here:
              the Bedrock connector on the Inference tab remains its source of truth, so it is never
              counted twice.
            </p>
          )}
          {summary.by_category.length > 0 && (
            <ul className="infra-categories">
              {summary.by_category.map((c) => (
                <li key={c.category}>
                  <span className="infra-category-name">
                    {CATEGORY_LABELS[c.category] ?? c.category}
                  </span>
                  <span className="infra-category-amount">{money(c.amount)}</span>
                </li>
              ))}
            </ul>
          )}
          {summary.services.length > 0 ? (
            <table className="data-table infra-services">
              <thead>
                <tr>
                  <th scope="col">AWS service</th>
                  <th scope="col">Spend</th>
                  <th scope="col">Attributed</th>
                </tr>
              </thead>
              <tbody>
                {summary.services.map((s) => (
                  <tr key={s.service}>
                    <td>{s.service || "(no service reported)"}</td>
                    <td>{money(s.amount)}</td>
                    <td>{money(s.attributed)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <p className="muted">
              No infrastructure line items for this month yet. Hit Sync now — AWS billing data lags
              a day or two behind.
            </p>
          )}
        </>
      )}
    </div>
  );
}

export function InfrastructureSources() {
  const [providers, setProviders] = useState<InfraProvider[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [openType, setOpenType] = useState<string | null>(null);
  const [detailVersion, setDetailVersion] = useState(0);

  const refresh = useCallback(async () => {
    try {
      setProviders(await api.infraProviders());
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load infrastructure providers.");
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  return (
    <section className="source-section" role="tabpanel">
      <p className="muted">
        What your product costs to run on cloud infrastructure — the databases, storage, networking
        and compute around your model calls. Connect a provider's cost API and we read the whole
        bill, classify every line item, and attribute spend to features by an activated
        cost-allocation tag. Nothing is dropped for being an unfamiliar service, and nothing is
        guessed: untagged spend lands in Unattributed.
      </p>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {providers && (
        <ul className="connector-list">
          {providers.map((p) =>
            p.status === "available" ? (
              <ConnectorRow
                key={p.type}
                connector={{
                  type: p.type,
                  name: p.name,
                  category: "infrastructure",
                  connected: p.connected,
                }}
                hint={statusLine(p)}
                onConnected={refresh}
                expanded={openType === p.type}
                onToggle={() => setOpenType((t) => (t === p.type ? null : p.type))}
                detail={<InfraDetail provider={p} refreshKey={detailVersion} />}
                onSync={async () => {
                  let r;
                  try {
                    r = await api.syncInfrastructure(p.type);
                  } catch (err) {
                    // A failed sync is still a sync: the backend recorded the
                    // run, so re-read the card rather than leaving it claiming
                    // whatever it said before this attempt.
                    await refresh();
                    throw err;
                  }
                  await refresh();
                  setDetailVersion((v) => v + 1);
                  const excluded =
                    r.excluded > 0
                      ? ` ${money(r.excluded)} of Bedrock spend was excluded — the Bedrock connector already counts it.`
                      : "";
                  return (
                    `Read ${r.items} line ${r.items === 1 ? "item" : "items"}: ` +
                    `${money(r.infrastructure)} of infrastructure cost.${excluded}`
                  );
                }}
              />
            ) : (
              <ComingSoonRow key={p.type} provider={p} />
            ),
          )}
        </ul>
      )}
    </section>
  );
}
