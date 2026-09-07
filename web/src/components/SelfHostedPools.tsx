/**
 * Self-hosted models — register a GPU/infra pool and allocate its monthly cost
 * across features by metered usage share.
 *
 * For teams running models on their own hardware — open-source (Llama, Mistral,
 * Qwen…) or any self-hosted deployment — there is no per-token bill; the cost is
 * the infra spend. We register each serving pool with its monthly cost, the
 * metering SDK reports per-feature usage tagged with the pool's label, and
 * `Allocate` splits the bill across features by usage share (see backend
 * compute.py). Usage with no feature lands in Unattributed.
 */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError, type ComputePool } from "../api";
import { money } from "../format";

export function SelfHostedPools({
  period,
  onChanged,
}: {
  period?: string;
  onChanged: () => Promise<void>;
}) {
  const [poolName, setPoolName] = useState("");
  const [poolLabel, setPoolLabel] = useState("");
  const [poolCost, setPoolCost] = useState("");
  const [pools, setPools] = useState<ComputePool[]>([]);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const monthParam = period?.slice(0, 7);

  useEffect(() => {
    api
      .listComputePools()
      .then(setPools)
      .catch(() => undefined);
  }, []);

  async function savePool() {
    const cost = parseFloat(poolCost);
    if (!poolName.trim() || !poolLabel.trim() || Number.isNaN(cost)) {
      setNote("Enter a model name, a usage label, and the monthly infra cost.");
      return;
    }
    setBusy(true);
    setNote(null);
    try {
      await api.createComputePool(poolName.trim(), poolLabel.trim(), cost);
      setPoolName("");
      setPoolLabel("");
      setPoolCost("");
      setPools(await api.listComputePools());
      setNote("Saved. It allocates to features once the SDK reports usage tagged with this label.");
    } catch (err) {
      setNote(err instanceof ApiError ? err.message : "Could not save pool.");
    } finally {
      setBusy(false);
    }
  }

  async function allocate() {
    setBusy(true);
    setNote(null);
    try {
      const res = await api.allocateCompute(monthParam);
      const total = res.reduce((sum, r) => sum + r.allocated, 0);
      setNote(
        res.length
          ? `Allocated ${money(total)} of self-hosted cost across features by usage.`
          : "No pools to allocate yet.",
      );
      await onChanged();
    } catch (err) {
      setNote(err instanceof ApiError ? err.message : "Allocation failed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <p className="muted">
        Running models on your own GPUs — open-source (Llama, Mistral, Qwen…) or any self-hosted
        deployment — has no per-token bill; the cost is your infra spend. Register each serving pool
        with its monthly cost, and Meter splits that cost across features by how much each one
        actually used the pool.
      </p>

      <div className="source-guide">
        <ol className="connector-steps">
          <li>Name the pool after the model or deployment (e.g. “Llama-3.1-70B”).</li>
          <li>
            Set a <strong>usage label</strong> — the identifier your app passes to the metering SDK
            as the call's <code>provider</code>. Usage tagged with this label rolls up to this pool.
          </li>
          <li>
            Enter the pool's <strong>monthly cost</strong> — the GPU / instance bill for serving it.
          </li>
          <li>
            Install the metering SDK so per-feature usage is recorded, then click{" "}
            <strong>Allocate cost</strong> to split the bill. Usage with no feature lands in
            Unattributed.
          </li>
        </ol>
        <Link to="/install-sdk" className="link connector-doc-link">
          Install the metering SDK →
        </Link>
      </div>

      <div className="data-action">
        <span className="inline">
          <input
            placeholder="Model / pool name (e.g. Llama-3.1-70B)"
            value={poolName}
            onChange={(e) => setPoolName(e.target.value)}
            autoComplete="off"
            aria-label="Pool name"
          />
          <input
            placeholder="Usage label (e.g. llama-70b)"
            value={poolLabel}
            onChange={(e) => setPoolLabel(e.target.value)}
            autoComplete="off"
            aria-label="Usage label"
          />
          <input
            placeholder="$ / month"
            inputMode="decimal"
            value={poolCost}
            onChange={(e) => setPoolCost(e.target.value)}
            autoComplete="off"
            aria-label="Monthly cost"
          />
          <button onClick={savePool} disabled={busy}>
            Save pool
          </button>
        </span>
      </div>

      {pools.length > 0 && (
        <div className="data-action">
          <label>Registered pools</label>
          <ul className="pool-list">
            {pools.map((p) => (
              <li key={p.id} className="pool-item">
                <span className="connector-name">{p.name}</span>
                <span className="muted">
                  label “{p.provider_label}” · {money(p.monthly_cost)}/mo
                </span>
              </li>
            ))}
          </ul>
          <button onClick={allocate} disabled={busy}>
            Allocate cost
          </button>
        </div>
      )}

      {note && <p className="muted">{note}</p>}
    </>
  );
}
