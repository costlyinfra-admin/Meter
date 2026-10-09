/**
 * Inline detail for one cost source — the single, provider-generic table shown
 * directly under an expanded source card.
 *
 * Purpose: inspect the resources a sync discovered, see each one's cost, and set
 * its classification (Production / Development-Test / Internal / Ignore /
 * Unclassified). No KPI tiles and no duplicated summaries — overall totals live on
 * Overview. Classification is a manual user choice; nothing is inferred from names.
 */
import { useCallback, useEffect, useState } from "react";
import {
  api,
  ApiError,
  CLASSIFICATION_OPTIONS,
  type Classification,
  type SourceDetail as Detail,
} from "../api";
import { money } from "../format";
import { Skeleton } from "./Skeleton";

export function SourceDetail({ provider, refreshKey }: { provider: string; refreshKey: number }) {
  const [detail, setDetail] = useState<Detail | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      setDetail(await api.sourceDetail(provider));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load source detail.");
    }
  }, [provider]);

  useEffect(() => {
    load();
  }, [load, refreshKey]);

  async function classify(row: Detail["rows"][number], value: Classification) {
    if (!row.resource_id) return;
    // Optimistic: reflect the choice immediately, then persist.
    setDetail((d) =>
      d ? { ...d, rows: d.rows.map((r) => (r === row ? { ...r, classification: value } : r)) } : d,
    );
    try {
      await api.classifyResource(provider, {
        resource_type: row.resource_type,
        resource_id: row.resource_id,
        classification: value,
      });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not save classification.");
      load(); // revert to server truth
    }
  }

  if (error) {
    return (
      <p className="error" role="alert">
        {error}
      </p>
    );
  }
  if (!detail) return <Skeleton />;

  if (!detail.classifiable) {
    return <p className="muted source-detail-empty">{detail.message}</p>;
  }
  if (detail.rows.length === 0) {
    return <p className="muted source-detail-empty">No resources discovered yet — run Sync now.</p>;
  }

  const cols = detail.columns ?? { group: "Group", name: "Resource" };
  return (
    <div className="source-detail">
      <div className="source-detail-head">
        <h4>
          {cols.group} &amp; {cols.name}
        </h4>
        <span className="muted source-detail-note">
          {detail.all_time
            ? "Every resource across your synced history — classify each one, you decide."
            : "Classify each resource — you decide."}
        </span>
      </div>
      <div className="source-detail-table-wrap">
        <table className="source-detail-table">
          <thead>
            <tr>
              <th>{cols.group}</th>
              <th>{cols.name}</th>
              <th>Classification</th>
              <th className="num">{detail.all_time ? "Cost (all-time)" : "Cost"}</th>
            </tr>
          </thead>
          <tbody>
            {detail.rows.map((r, i) => (
              <tr key={`${r.resource_type}-${r.resource_id}-${i}`}>
                <td className="mono">{r.group ?? "—"}</td>
                <td className="mono">{r.name ?? "—"}</td>
                <td>
                  {r.resource_id ? (
                    <select
                      className={`class-select class-${r.classification}`}
                      value={r.classification}
                      aria-label={`Classification for ${r.name ?? r.resource_id}`}
                      onChange={(e) => classify(r, e.target.value as Classification)}
                    >
                      {CLASSIFICATION_OPTIONS.map((o) => (
                        <option key={o.value} value={o.value}>
                          {o.label}
                        </option>
                      ))}
                    </select>
                  ) : (
                    <span className="muted">—</span>
                  )}
                </td>
                <td className="num">{money(r.cost)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
