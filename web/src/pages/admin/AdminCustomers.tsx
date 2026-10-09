/** Admin customer list — one row per tenant, linking to the support detail view. */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, type AdminCustomer } from "../../api";
import { money, shortDate } from "../../format";
import { Skeleton } from "../../components/Skeleton";

export function AdminCustomers() {
  const [rows, setRows] = useState<AdminCustomer[] | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    api
      .adminCustomers()
      .then(setRows)
      .catch(() => setError(true));
  }, []);

  return (
    <div className="content">
      <div className="dash-head">
        <h1>Customers</h1>
      </div>

      {error ? (
        <p className="error" role="alert">
          Could not load customers.
        </p>
      ) : rows === null ? (
        <Skeleton />
      ) : rows.length === 0 ? (
        <p className="muted">No customers yet.</p>
      ) : (
        <table className="mini-table">
          <thead>
            <tr>
              <th>Company</th>
              <th>Status</th>
              <th>Connected providers</th>
              <th>Last sync</th>
              <th className="num">Monthly spend</th>
              <th className="num">Opportunities</th>
              <th className="num">Verified savings</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((c) => (
              <tr key={c.tenant_id}>
                <td>
                  <Link to={`/admin/customers/${c.tenant_id}`} className="link">
                    {c.company}
                  </Link>
                </td>
                <td>
                  <span className={`badge ${c.status === "connected" ? "connected" : ""}`}>
                    {c.status}
                  </span>
                </td>
                <td>{c.connected_providers.length ? c.connected_providers.join(", ") : "—"}</td>
                <td className="muted">{c.last_sync ? shortDate(c.last_sync) : "never"}</td>
                <td className="num">{money(c.monthly_spend)}/mo</td>
                <td className="num">{c.opportunities}</td>
                <td className="num">{money(c.verified_savings)}/mo</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
