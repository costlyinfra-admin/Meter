/**
 * Products — the customer's own grouping above features.
 *
 * The job of this page is to make mapping 20-odd repositories into 5-6 products
 * take a couple of minutes rather than an afternoon. It offers products derived
 * from repository names ("sentinel-api", "sentinel-web" -> Sentinel), which the
 * customer accepts or ignores; nothing is ever created without a click.
 *
 * Assignment is derived, not typed in: a feature belongs to the product that
 * owns the repositories its pull requests came from. Where the repositories
 * disagree, the feature is listed here for a person to decide rather than
 * guessed into one of them.
 */
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  api,
  ApiError,
  type Product,
  type ProductSuggestions,
  type ReassignResult,
  type SpanningFeature,
} from "../api";

export function ProductsPage() {
  const [products, setProducts] = useState<Product[] | null>(null);
  const [suggested, setSuggested] = useState<ProductSuggestions | null>(null);
  const [spanning, setSpanning] = useState<SpanningFeature[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [applied, setApplied] = useState<ReassignResult | null>(null);
  const [name, setName] = useState("");

  const load = useCallback(async () => {
    try {
      const [list, hints, spans] = await Promise.all([
        api.listProducts(),
        api.productSuggestions(),
        api.spanningFeatures(),
      ]);
      setProducts(list.products);
      setSuggested(hints);
      setSpanning(spans.features);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load products.");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const run = async (key: string, work: () => Promise<unknown>) => {
    setBusy(key);
    setError(null);
    try {
      await work();
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "That didn't work.");
    } finally {
      setBusy(null);
    }
  };

  const create = (event: React.FormEvent) => {
    event.preventDefault();
    const wanted = name.trim();
    if (!wanted) return;
    void run("create", async () => {
      await api.createProduct(wanted);
      setName("");
    });
  };

  return (
    <div className="content">
      <div className="dash-head">
        <h1>Products</h1>
      </div>
      <p className="muted">
        A product is a thing you sell. Meter groups your features into products — and rolls up what
        each one cost to build and to run — from the repositories their pull requests came from.
      </p>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      <section className="detail-section">
        <div className="section-head">
          <div>
            <h2>Your products</h2>
            <span className="section-sub muted">
              Tick the repositories each product is built in. A repository belongs to one product;
              for something genuinely shared, make a product for it ("Platform").
            </span>
          </div>
          <button
            onClick={() =>
              void run("reassign", async () => setApplied(await api.reassignProducts()))
            }
            disabled={busy !== null}
          >
            {busy === "reassign" ? "Applying…" : "Re-apply repository mapping"}
          </button>
        </div>

        {applied && (
          <p className="hint">
            {applied.assigned} feature{applied.assigned === 1 ? "" : "s"} assigned,{" "}
            {applied.unassigned} left unassigned
            {applied.spanning > 0 && `, of which ${applied.spanning} span more than one product`}.
          </p>
        )}

        <form className="inline-form" onSubmit={create}>
          <label htmlFor="new-product">New product</label>
          <input
            id="new-product"
            value={name}
            placeholder="Sentinel"
            onChange={(e) => setName(e.target.value)}
          />
          <button type="submit" disabled={busy !== null || !name.trim()}>
            {busy === "create" ? "Adding…" : "Add product"}
          </button>
        </form>

        {products === null ? (
          <p className="muted">Loading…</p>
        ) : products.length === 0 ? (
          <p className="muted">
            No products yet. Add one above, or accept a suggestion below — then every feature built
            in its repositories rolls up into it.
          </p>
        ) : (
          products.map((product) => (
            <ProductCard
              key={product.id}
              product={product}
              candidates={[...product.repos, ...(suggested?.unmapped ?? [])].sort()}
              busy={busy}
              onRepos={(repos) =>
                run(`repos-${product.id}`, async () => {
                  const result = await api.setProductRepos(product.id, repos);
                  setApplied(result.reassigned);
                })
              }
              onDelete={() => run(`del-${product.id}`, () => api.deleteProduct(product.id))}
            />
          ))
        )}
      </section>

      {suggested && suggested.suggestions.length > 0 && (
        <section className="detail-section">
          <h2>Suggested from your repository names</h2>
          <p className="section-sub muted">
            Grouped by the first word of each repository. These are guesses — accepting one creates
            the product and maps those repositories; nothing happens until you do.
          </p>
          <table className="features-table">
            <thead>
              <tr>
                <th>Suggested product</th>
                <th>Repositories</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {suggested.suggestions.map((hint) => (
                <tr key={hint.name}>
                  <td>{hint.name}</td>
                  <td className="muted">{hint.repos.join(", ")}</td>
                  <td className="num">
                    <button
                      className="secondary"
                      disabled={busy !== null}
                      onClick={() =>
                        void run(`hint-${hint.name}`, async () => {
                          const made = await api.createProduct(hint.name);
                          const result = await api.setProductRepos(made.id, hint.repos);
                          setApplied(result.reassigned);
                        })
                      }
                    >
                      Create
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {spanning.length > 0 && (
        <section className="detail-section">
          <h2>Features that span more than one product</h2>
          <p className="section-sub muted">
            These were built in repositories belonging to different products, so Meter will not
            choose for you — a guess here would be a number you could not check. Pick a product on
            the feature itself, and your choice is kept.
          </p>
          <table className="features-table">
            <thead>
              <tr>
                <th>Feature</th>
                <th>Repositories</th>
                <th>Could be</th>
              </tr>
            </thead>
            <tbody>
              {spanning.map((feature) => (
                <tr key={feature.feature_id}>
                  <td>
                    <Link to={`/features/${feature.feature_id}`} className="link">
                      {feature.name}
                    </Link>
                  </td>
                  <td className="muted">{feature.repos.join(", ")}</td>
                  <td className="muted">{feature.products.join(" or ")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
    </div>
  );
}

/** One product, with the repositories it is built in. */
function ProductCard({
  product,
  candidates,
  busy,
  onRepos,
  onDelete,
}: {
  product: Product;
  candidates: string[];
  busy: string | null;
  onRepos: (repos: string[]) => Promise<void>;
  onDelete: () => Promise<void>;
}) {
  const [chosen, setChosen] = useState<string[]>(product.repos);
  const [confirming, setConfirming] = useState(false);

  useEffect(() => setChosen(product.repos), [product.repos]);

  const toggle = (repo: string) =>
    setChosen((was) => (was.includes(repo) ? was.filter((r) => r !== repo) : [...was, repo]));

  const changed =
    [...chosen].sort().join("|") !== [...product.repos].sort().join("|") && busy === null;

  return (
    <div className="detail-card">
      <div className="section-head">
        <div>
          <h3>{product.name}</h3>
          <span className="section-sub muted">
            {product.feature_count} feature{product.feature_count === 1 ? "" : "s"} ·{" "}
            {product.repos.length} repositor{product.repos.length === 1 ? "y" : "ies"}
          </span>
        </div>
        {confirming ? (
          <span className="confirm-row">
            <span className="muted">Delete {product.name}? Its features stay, unassigned.</span>
            <button className="danger" onClick={() => void onDelete()} disabled={busy !== null}>
              Delete
            </button>
            <button className="secondary" onClick={() => setConfirming(false)}>
              Keep
            </button>
          </span>
        ) : (
          <button
            className="secondary"
            onClick={() => setConfirming(true)}
            disabled={busy !== null}
          >
            Delete
          </button>
        )}
      </div>

      {candidates.length === 0 ? (
        <p className="muted">
          No repositories to map yet — they appear once discovery has run.{" "}
          <Link to="/features" className="link">
            Run discovery
          </Link>
        </p>
      ) : (
        <ul className="repo-list">
          {candidates.map((repo) => (
            <li key={repo}>
              <label>
                <input
                  type="checkbox"
                  checked={chosen.includes(repo)}
                  onChange={() => toggle(repo)}
                />{" "}
                {repo}
              </label>
            </li>
          ))}
        </ul>
      )}

      <button onClick={() => void onRepos(chosen)} disabled={!changed}>
        {busy === `repos-${product.id}` ? "Saving…" : "Save repositories"}
      </button>
    </div>
  );
}
