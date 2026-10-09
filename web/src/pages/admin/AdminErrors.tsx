/** Admin errors — connector failures across all customers, with messages. */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, type AdminSyncRow } from "../../api";
import { shortDate } from "../../format";
import { Skeleton } from "../../components/Skeleton";

export function AdminErrors() {
  const [rows, setRows] = useState<AdminSyncRow[] | null>(null);

  useEffect(() => {
    api
      .adminErrors()
      .then(setRows)
      .catch(() => setRows([]));
  }, []);

  return (
    <div className="content">
      <div className="dash-head">
        <h1>Errors</h1>
      </div>
      <p className="muted">Connector, authentication and rate-limit failures.</p>
      {rows === null ? (
        <Skeleton />
      ) : rows.length === 0 ? (
        <p className="muted">No errors recorded. 🎉</p>
      ) : (
        <table className="mini-table">
          <thead>
            <tr>
              <th>Customer</th>
              <th>Connector</th>
              <th>Action</th>
              <th>When</th>
              <th>Error</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i}>
                <td>
                  <Link to={`/admin/customers/${r.tenant_id}`} className="link">
                    {r.company}
                  </Link>
                </td>
                <td>{r.connector_type}</td>
                <td>{r.action}</td>
                <td className="muted">{shortDate(r.started_at)}</td>
                <td className="admin-error-cell">{r.error_message}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
