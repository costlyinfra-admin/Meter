/**
 * Admin customer detail — the support hub for one tenant: company info, connector
 * management (Test / Sync / Disconnect / Add), repositories, recent optimization
 * runs, sync history and errors, plus "View customer portal" (impersonation).
 * All actions call the shared admin services; no customer pages are duplicated.
 */
import { useCallback, useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  api,
  type AdminCustomerDetail as Detail,
  type ConnectorActionResult,
  type ConnectorStatus,
} from "../../api";
import { useAuth } from "../../auth/AuthContext";
import { money, shortDate } from "../../format";
import { Skeleton } from "../../components/Skeleton";

// Connector types an admin can add from the portal (the brief's initial set).
const ADDABLE = [
  { type: "anthropic", name: "Anthropic" },
  { type: "github", name: "GitHub" },
  { type: "openai", name: "OpenAI" },
  { type: "google", name: "Vertex / Gemini" },
  { type: "bedrock", name: "Amazon Bedrock" },
  { type: "azure", name: "Azure OpenAI" },
];

export function AdminCustomerDetail() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const { refresh } = useAuth();
  const [detail, setDetail] = useState<Detail | null>(null);
  const [error, setError] = useState(false);
  const [results, setResults] = useState<Record<string, ConnectorActionResult>>({});
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setDetail(await api.adminCustomer(id));
    } catch {
      setError(true);
    }
  }, [id]);
  useEffect(() => {
    load();
  }, [load]);

  const runAction = async (type: string, action: "test" | "sync") => {
    setBusy(`${type}:${action}`);
    try {
      const res =
        action === "test"
          ? await api.adminTestConnector(id, type)
          : await api.adminSyncConnector(id, type);
      setResults((r) => ({ ...r, [type]: res }));
      await load();
    } finally {
      setBusy(null);
    }
  };

  const disconnect = async (type: string) => {
    setBusy(`${type}:disconnect`);
    try {
      await api.adminDisconnectConnector(id, type);
      await load();
    } finally {
      setBusy(null);
    }
  };

  const viewPortal = async () => {
    await api.impersonate(id);
    await refresh();
    navigate("/");
  };

  if (error)
    return (
      <div className="content">
        <p className="error">Could not load customer.</p>
      </div>
    );
  if (!detail)
    return (
      <div className="content">
        <Skeleton />
      </div>
    );

  const connected = detail.connectors.filter((c) => c.connected);

  return (
    <div className="content">
      <div className="section-head">
        <div>
          <h1>{detail.company}</h1>
          <span className="section-sub muted">
            {detail.users.join(", ") || "no users"} ·{" "}
            {detail.created_at ? `joined ${shortDate(detail.created_at)}` : ""}
          </span>
        </div>
        <button className="primary" onClick={viewPortal}>
          View customer portal →
        </button>
      </div>

      <ConnectorSection
        connected={connected}
        results={results}
        busy={busy}
        onAction={runAction}
        onDisconnect={disconnect}
        onSave={load}
        tenantId={id}
      />

      <div className="copilot-cols">
        <section className="detail-section">
          <div className="section-head">
            <h2>Repositories</h2>
          </div>
          {detail.repositories.length === 0 ? (
            <p className="muted">None connected.</p>
          ) : (
            <ul className="admin-list">
              {detail.repositories.map((r) => (
                <li key={r}>{r}</li>
              ))}
            </ul>
          )}
        </section>

        <section className="detail-section">
          <div className="section-head">
            <h2>Recent optimization runs</h2>
          </div>
          {detail.optimization_runs.length === 0 ? (
            <p className="muted">No optimizations applied yet.</p>
          ) : (
            <table className="mini-table">
              <thead>
                <tr>
                  <th>Lever</th>
                  <th>Applied</th>
                  <th className="num">Projected</th>
                </tr>
              </thead>
              <tbody>
                {detail.optimization_runs.map((o, i) => (
                  <tr key={i}>
                    <td>{o.lever.replace(/_/g, " ")}</td>
                    <td className="muted">{shortDate(o.created_at)}</td>
                    <td className="num">{money(o.projected_monthly)}/mo</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      </div>

      <SyncTable title="Recent sync history" rows={detail.recent_syncs} />
      {detail.recent_errors.length > 0 && (
        <SyncTable title="Recent errors" rows={detail.recent_errors} showError />
      )}
    </div>
  );
}

function ConnectorSection({
  connected,
  results,
  busy,
  onAction,
  onDisconnect,
  onSave,
  tenantId,
}: {
  connected: ConnectorStatus[];
  results: Record<string, ConnectorActionResult>;
  busy: string | null;
  onAction: (type: string, action: "test" | "sync") => void;
  onDisconnect: (type: string) => void;
  onSave: () => Promise<void>;
  tenantId: string;
}) {
  const [type, setType] = useState("anthropic");
  const [secret, setSecret] = useState("");
  const [label, setLabel] = useState("");
  const [saving, setSaving] = useState(false);

  const save = async () => {
    if (!secret.trim()) return;
    setSaving(true);
    try {
      await api.adminSaveConnector(tenantId, type, secret.trim(), label.trim() || undefined);
      setSecret("");
      setLabel("");
      await onSave();
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="detail-section">
      <div className="section-head">
        <h2>Connectors</h2>
      </div>

      {connected.length === 0 ? (
        <p className="muted">No connectors configured.</p>
      ) : (
        <table className="mini-table">
          <thead>
            <tr>
              <th>Connector</th>
              <th>Status</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {connected.map((c) => {
              const res = results[c.type];
              return (
                <tr key={c.type}>
                  <td>{c.name}</td>
                  <td>
                    {res ? (
                      <span className={res.status === "success" ? "opt-realized" : "error"}>
                        {res.status === "success"
                          ? `ok${res.records_imported != null ? ` · ${res.records_imported} records` : ""}`
                          : `error: ${res.error_message ?? ""}`}
                      </span>
                    ) : (
                      <span className="badge connected">connected</span>
                    )}
                  </td>
                  <td className="admin-actions">
                    <button
                      className="opt-apply-btn"
                      disabled={busy !== null}
                      onClick={() => onAction(c.type, "test")}
                    >
                      {busy === `${c.type}:test` ? "Testing…" : "Test"}
                    </button>
                    <button
                      className="opt-apply-btn"
                      disabled={busy !== null}
                      onClick={() => onAction(c.type, "sync")}
                    >
                      {busy === `${c.type}:sync` ? "Syncing…" : "Sync now"}
                    </button>
                    <button
                      className="opt-apply-btn"
                      disabled={busy !== null}
                      onClick={() => onDisconnect(c.type)}
                    >
                      Disconnect
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}

      <div className="admin-add-connector">
        <select value={type} onChange={(e) => setType(e.target.value)} aria-label="Connector type">
          {ADDABLE.map((c) => (
            <option key={c.type} value={c.type}>
              {c.name}
            </option>
          ))}
        </select>
        <input
          type="password"
          placeholder="API key / token (encrypted at rest)"
          value={secret}
          onChange={(e) => setSecret(e.target.value)}
        />
        <input
          type="text"
          placeholder="Label (e.g. GitHub org)"
          value={label}
          onChange={(e) => setLabel(e.target.value)}
        />
        <button className="secondary" onClick={save} disabled={saving || !secret.trim()}>
          {saving ? "Saving…" : "Add connector"}
        </button>
      </div>
    </section>
  );
}

function SyncTable({
  title,
  rows,
  showError,
}: {
  title: string;
  rows: Detail["recent_syncs"];
  showError?: boolean;
}) {
  return (
    <section className="detail-section">
      <div className="section-head">
        <h2>{title}</h2>
      </div>
      {rows.length === 0 ? (
        <p className="muted">Nothing yet.</p>
      ) : (
        <table className="mini-table">
          <thead>
            <tr>
              <th>Connector</th>
              <th>Action</th>
              <th>Started</th>
              <th className="num">Records</th>
              <th>Status</th>
              {showError && <th>Error</th>}
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i}>
                <td>{r.connector_type}</td>
                <td>{r.action}</td>
                <td className="muted">{shortDate(r.started_at)}</td>
                <td className="num">{r.records_imported ?? "—"}</td>
                <td>
                  <span className={r.status === "success" ? "opt-realized" : "error"}>
                    {r.status}
                  </span>
                </td>
                {showError && <td className="muted">{r.error_message}</td>}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
