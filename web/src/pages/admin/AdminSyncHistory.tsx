/** Admin sync history — every connector Test/Sync run across all customers. */
import { useEffect, useState } from "react";
import { api, type AdminSyncRow } from "../../api";
import { shortDate } from "../../format";
import { Skeleton } from "../../components/Skeleton";

export function AdminSyncHistory() {
  const [rows, setRows] = useState<AdminSyncRow[] | null>(null);

  useEffect(() => {
    api
      .adminSyncHistory()
      .then(setRows)
      .catch(() => setRows([]));
  }, []);

  return (
    <div className="content">
      <div className="dash-head">
        <h1>Sync history</h1>
      </div>
      {rows === null ? (
        <Skeleton />
      ) : rows.length === 0 ? (
        <p className="muted">No syncs run yet.</p>
      ) : (
        <table className="mini-table">
          <thead>
            <tr>
              <th>Customer</th>
              <th>Connector</th>
              <th>Action</th>
              <th>Started</th>
              <th>Finished</th>
              <th className="num">Duration</th>
              <th className="num">Records</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i}>
                <td>{r.company}</td>
                <td>{r.connector_type}</td>
                <td>{r.action}</td>
                <td className="muted">{shortDate(r.started_at)}</td>
                <td className="muted">{r.finished_at ? shortDate(r.finished_at) : "—"}</td>
                <td className="num">{r.duration_ms != null ? `${r.duration_ms} ms` : "—"}</td>
                <td className="num">{r.records_imported ?? "—"}</td>
                <td>
                  <span className={r.status === "success" ? "opt-realized" : "error"}>
                    {r.status}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
