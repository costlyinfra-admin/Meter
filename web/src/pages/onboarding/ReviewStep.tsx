/**
 * Review auto-discovered features (part of the "Identify features" step).
 *
 * Runs discovery against a GitHub org, then lets the user curate the proposals:
 * rename, delete, split (one proposal is really two), merge (two are one), and
 * add a feature manually. Each proposal shows its PR/branch evidence, a
 * discovery-confidence badge, and the product surface it belongs to (which
 * discovery guesses from the PRs and the user can correct here).
 */
import { useEffect, useState } from "react";
import { DiscoverySchedule } from "../../components/DiscoverySchedule";
import { api, ApiError, type Feature, type Product } from "../../api";
import { CategoryBadge, ProductBadge } from "../../components/badges";
import { CategoryPicker } from "../../components/CategoryPicker";
import { ProductPicker } from "../../components/ProductPicker";

const NEEDS_REVIEW = "Needs review";

function ConfidenceBadge({ level }: { level: string | null }) {
  const l = level ?? "low";
  return <span className={`badge conf-${l}`}>{l} confidence</span>;
}

export function ReviewStep() {
  const [features, setFeatures] = useState<Feature[] | null>(null);
  const [owner, setOwner] = useState("");
  const [summary, setSummary] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [products, setProducts] = useState<Product[] | null>(null);

  // Repo selector: the org's repositories, and which the user picked to analyze.
  const [repoList, setRepoList] = useState<string[] | null>(null);
  const [selectedRepos, setSelectedRepos] = useState<Set<string>>(new Set());
  const [loadingRepos, setLoadingRepos] = useState(false);

  const reload = async () => setFeatures(await api.listFeatures("proposed"));

  useEffect(() => {
    reload().catch(() => setFeatures([]));
    api
      .listProducts()
      .then((d) => setProducts(d.products))
      .catch(() => setProducts([]));
    // Prefill the last-used org + repo selection so re-runs are one click.
    api
      .discoveryScope()
      .then((scope) => {
        if (scope.owner) setOwner(scope.owner);
        if (scope.repos.length) setSelectedRepos(new Set(scope.repos));
      })
      .catch(() => {});
  }, []);

  async function loadRepos() {
    const who = owner.trim();
    if (!who) return;
    setLoadingRepos(true);
    setError(null);
    try {
      const { repos } = await api.discoveryRepos(who);
      setRepoList(repos);
      // Keep any previously-saved selection that still exists in this org.
      setSelectedRepos((prev) => new Set([...prev].filter((r) => repos.includes(r))));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not list repositories.");
      setRepoList(null);
    } finally {
      setLoadingRepos(false);
    }
  }

  function toggleRepo(repo: string) {
    setSelectedRepos((prev) => {
      const next = new Set(prev);
      if (next.has(repo)) next.delete(repo);
      else next.add(repo);
      return next;
    });
  }

  async function runDiscovery() {
    setBusy(true);
    setError(null);
    try {
      const scope = [...selectedRepos];
      const s = await api.runDiscovery(owner.trim(), scope);
      const who = owner.trim();
      if (s.repos_scanned === 0) {
        setSummary(
          `No repositories accessible for "${who}". Check the token has repo access ` +
            `(private repos need a classic PAT with the "repo" scope) and that "${who}" is ` +
            `the org/user login.`,
        );
      } else if (s.prs === 0) {
        const n = s.repos.length || s.repos_scanned;
        setSummary(
          `Analyzed ${n} repositor${n === 1 ? "y" : "ies"} for "${who}", but found no merged PRs ` +
            `in the last 90 days — discovery needs merged pull requests.`,
        );
      } else {
        const label =
          s.repos.length === 1
            ? s.repos[0]
            : `${s.repos.length} repositories${scope.length ? " (selected)" : ""}`;
        setSummary(
          `Repository: ${label} · Analyzed: ${s.prs} merged PRs · ` +
            `Proposed: ${s.proposals} feature${s.proposals === 1 ? "" : "s"}.`,
        );
      }
      setSelected(new Set());
      await reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Discovery failed. Try again.");
    } finally {
      setBusy(false);
    }
  }

  function toggleSelect(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  async function mergeSelected() {
    await api.mergeFeatures([...selected]);
    setSelected(new Set());
    await reload();
  }

  const scopeCount = selectedRepos.size;

  return (
    <div>
      <h2>Review auto-discovered features</h2>
      <p className="muted">
        Meter analyzes your last 90 days of merged pull requests and proposes features. Pick the
        repositories to analyze, curate the proposals below, then confirm.
      </p>

      <div className="discovery-bar">
        <input
          placeholder="GitHub organization (e.g. acme)"
          value={owner}
          onChange={(e) => {
            setOwner(e.target.value);
            setRepoList(null); // org changed — force a fresh repo list
          }}
          aria-label="GitHub organization"
        />
        <button className="secondary" onClick={loadRepos} disabled={loadingRepos || !owner.trim()}>
          {loadingRepos ? "Loading…" : "List repositories"}
        </button>
        <button onClick={runDiscovery} disabled={busy || !owner.trim()}>
          {busy
            ? "Analyzing…"
            : scopeCount
              ? `Analyze ${scopeCount} selected`
              : "Analyze last 90 days"}
        </button>
      </div>

      {repoList && (
        <div className="repo-selector">
          <p className="muted repo-selector-hint">
            {repoList.length === 0
              ? "No repositories accessible for this org/token."
              : `Select repositories to analyze (${scopeCount || "all"} selected). Leave all ` +
                `unchecked to analyze the whole org.`}
          </p>
          <ul className="repo-list">
            {repoList.map((repo) => (
              <li key={repo}>
                <label>
                  <input
                    type="checkbox"
                    checked={selectedRepos.has(repo)}
                    onChange={() => toggleRepo(repo)}
                  />
                  {repo}
                </label>
              </li>
            ))}
          </ul>
        </div>
      )}

      <p className="muted hint-inline">
        No token needed for <strong>public</strong> organizations. Connect GitHub above for private
        repos and higher rate limits.
      </p>
      {summary && <p className="summary">{summary}</p>}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      {/* How current this list is, and the nightly switch — directly above the
          list's own controls, because "is this up to date?" is the question
          someone has while looking at it. */}
      <DiscoverySchedule />

      {features === null ? (
        <p className="muted">Loading…</p>
      ) : features.length === 0 ? (
        <div className="empty-state">
          <p className="empty-title">No features discovered yet</p>
          <p className="muted">
            Enter a GitHub organization above and run analysis. Public orgs work without a token;
            connect GitHub above for private repos. Proposals appear here with their PR evidence and
            a confidence badge.
          </p>
        </div>
      ) : (
        <>
          <div className="review-toolbar">
            <AddFeature onAdded={reload} />
            {selected.size >= 2 && (
              <button onClick={mergeSelected}>Merge selected ({selected.size})</button>
            )}
          </div>
          <ul className="feature-list">
            {features.map((f) => (
              <FeatureCard
                key={f.id}
                feature={f}
                products={products}
                selected={selected.has(f.id)}
                onToggleSelect={() => toggleSelect(f.id)}
                onChanged={reload}
              />
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

function AddFeature({ onAdded }: { onAdded: () => Promise<void> }) {
  const [name, setName] = useState("");
  async function add() {
    if (!name.trim()) return;
    await api.addFeature(name.trim());
    setName("");
    await onAdded();
  }
  return (
    <span className="add-feature">
      <input
        placeholder="Add a feature manually"
        value={name}
        onChange={(e) => setName(e.target.value)}
        aria-label="New feature name"
      />
      <button className="secondary" onClick={add} disabled={!name.trim()}>
        Add
      </button>
    </span>
  );
}

function FeatureCard({
  feature,
  products,
  selected,
  onToggleSelect,
  onChanged,
}: {
  feature: Feature;
  products: Product[] | null;
  selected: boolean;
  onToggleSelect: () => void;
  onChanged: () => Promise<void>;
}) {
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(feature.name);
  const [splitting, setSplitting] = useState(false);

  const prSignals = feature.signals.filter((s) => s.signal_type === "pr");
  const branchSignal = feature.signals.find((s) => s.signal_type === "branch");

  async function saveName() {
    await api.renameFeature(feature.id, { name: name.trim() || feature.name });
    setEditing(false);
    await onChanged();
  }

  const isReview = feature.name === NEEDS_REVIEW;

  return (
    <li className={`feature-card${isReview ? " needs-review" : ""}`}>
      <div className="feature-head">
        <input
          type="checkbox"
          checked={selected}
          onChange={onToggleSelect}
          aria-label={`Select ${feature.name}`}
        />
        {editing ? (
          <span className="rename-row">
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              aria-label="Feature name"
            />
            <button onClick={saveName}>Save</button>
            <button className="secondary" onClick={() => setEditing(false)}>
              Cancel
            </button>
          </span>
        ) : (
          <>
            <span className="feature-name">{feature.name}</span>
            {isReview ? (
              <span className="badge conf-review">needs review</span>
            ) : (
              <ConfidenceBadge level={feature.discovery_confidence} />
            )}
            <ProductBadge product={feature.product_name} source={feature.product_source} />
            <CategoryBadge category={feature.category} source={feature.category_source} />
            <span className="feature-actions">
              <ProductPicker
                value={feature.product_id}
                products={products}
                onChange={async (productId) => {
                  await api.setFeatureProduct(feature.id, productId);
                  await onChanged();
                }}
              />
              <CategoryPicker
                value={feature.category}
                onChange={async (category) => {
                  await api.setFeatureCategory(feature.id, category);
                  await onChanged();
                }}
              />
              <button className="link" onClick={() => setEditing(true)}>
                Rename
              </button>
              {prSignals.length > 1 && (
                <button className="link" onClick={() => setSplitting((v) => !v)}>
                  Split
                </button>
              )}
              <button
                className="link danger"
                onClick={async () => {
                  await api.deleteFeature(feature.id);
                  await onChanged();
                }}
              >
                Delete
              </button>
            </span>
          </>
        )}
      </div>

      {branchSignal && <p className="branch-pattern">branch: {branchSignal.external_ref}</p>}
      <ul className="pr-evidence">
        {prSignals.map((s) => (
          <li key={s.id} className="pr-row">
            {s.url ? (
              <a className="pr-ref" href={s.url} target="_blank" rel="noreferrer">
                {s.external_ref}
              </a>
            ) : (
              <span className="pr-ref">{s.external_ref}</span>
            )}
            {s.title && <span className="pr-title">{s.title}</span>}
            {s.branch && <span className="pr-branch">{s.branch}</span>}
          </li>
        ))}
      </ul>

      {splitting && (
        <SplitForm
          feature={feature}
          onDone={async () => {
            setSplitting(false);
            await onChanged();
          }}
        />
      )}
    </li>
  );
}

function SplitForm({ feature, onDone }: { feature: Feature; onDone: () => Promise<void> }) {
  const prSignals = feature.signals.filter((s) => s.signal_type === "pr");
  const [moved, setMoved] = useState<Set<string>>(new Set());
  const [newName, setNewName] = useState("");

  function toggle(id: string) {
    setMoved((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  async function apply() {
    const movedIds = [...moved];
    const restIds = prSignals.filter((s) => !moved.has(s.id)).map((s) => s.id);
    if (movedIds.length === 0 || restIds.length === 0 || !newName.trim()) return;
    await api.splitFeature(feature.id, [
      { name: newName.trim(), signal_ids: movedIds },
      { name: `${feature.name} (rest)`, signal_ids: restIds },
    ]);
    await onDone();
  }

  return (
    <div className="split-form">
      <p className="muted">Pick the PRs to peel into a new feature:</p>
      <ul className="split-prs">
        {prSignals.map((s) => (
          <li key={s.id}>
            <label>
              <input type="checkbox" checked={moved.has(s.id)} onChange={() => toggle(s.id)} />
              {s.external_ref}
            </label>
          </li>
        ))}
      </ul>
      <span className="split-apply">
        <input
          placeholder="New feature name"
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
          aria-label="Split feature name"
        />
        <button onClick={apply} disabled={!newName.trim() || moved.size === 0}>
          Split
        </button>
      </span>
    </div>
  );
}
