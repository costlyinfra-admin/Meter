/**
 * Wizard Step 1 — identify features: connect GitHub, then discover + curate.
 *
 * Features are the spine everything attributes to, so this step combines the
 * GitHub connection with discovery/curation (ReviewStep) for immediate payoff.
 */
import { useEffect, useState } from "react";
import { api, ApiError, type ConnectorStatus } from "../../api";
import { ConnectorRow } from "../../components/ConnectorRow";
import { DiscoverySchedule } from "../../components/DiscoverySchedule";
import { ReviewStep } from "./ReviewStep";

export function FeaturesStep() {
  const [connectors, setConnectors] = useState<ConnectorStatus[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [openType, setOpenType] = useState<string | null>(null);

  async function refresh() {
    try {
      setConnectors(await api.connectors());
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load connectors.");
    }
  }

  useEffect(() => {
    refresh();
  }, []);

  const featureSources = (connectors ?? []).filter((c) => c.category === "features");

  return (
    <div>
      <h2>Identify features</h2>
      <p className="muted">
        Annapurna reads your merged pull requests to propose the features you've shipped — the spine
        that every cost attributes to. A token is optional for public organizations and required for
        private repos. Read-only, stored encrypted.
      </p>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {connectors !== null && featureSources.length > 0 && (
        <ul className="connector-list">
          {featureSources.map((c) => (
            <ConnectorRow
              key={c.type}
              connector={c}
              onConnected={refresh}
              hint="feature discovery · optional for public orgs"
              expanded={openType === c.type}
              onToggle={() => setOpenType((t) => (t === c.type ? null : c.type))}
            />
          ))}
        </ul>
      )}

      <ReviewStep />

      <DiscoverySchedule />
    </div>
  );
}
