/** Admin dashboard — tenant-wide KPIs, aggregated from existing services. */
import { useEffect, useState } from "react";
import { api, type AdminOverview } from "../../api";
import { compact, money } from "../../format";
import { Skeleton } from "../../components/Skeleton";

export function AdminDashboard() {
  const [data, setData] = useState<AdminOverview | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    api
      .adminOverview()
      .then(setData)
      .catch(() => setError(true));
  }, []);

  return (
    <div className="content">
      <div className="dash-head">
        <h1>Admin dashboard</h1>
      </div>
      <p className="muted">Onboarding and support at a glance — across every customer.</p>

      {error ? (
        <p className="error" role="alert">
          Could not load the dashboard.
        </p>
      ) : data === null ? (
        <Skeleton variant="page" />
      ) : (
        <div className="copilot-kpis admin-kpis">
          <Stat label="Total customers" value={compact(data.total_customers)} />
          <Stat label="Connected" value={compact(data.connected_customers)} tone="measured" />
          <Stat
            label="Pending connections"
            value={compact(data.pending_connections)}
            tone="ceiling"
          />
          <Stat label="Total AI spend" value={`${money(data.total_ai_spend)}/mo`} />
          <Stat label="Total opportunities" value={compact(data.total_opportunities)} />
          <Stat
            label="Verified savings"
            value={`${money(data.total_verified_savings)}/yr`}
            tone="verified"
          />
        </div>
      )}
    </div>
  );
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div className={`copilot-kpi${tone ? ` kpi-${tone}` : ""}`}>
      <span className="copilot-kpi-label">{label}</span>
      <span className="copilot-kpi-value">{value}</span>
    </div>
  );
}
