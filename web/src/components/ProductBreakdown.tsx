/**
 * Overview "By Product" tab — what each product the customer sells costs them.
 *
 * A product is a grouping the customer defines above features, so this is the
 * feature rollup re-keyed by product. Build and inference stay in separate
 * columns, as everywhere else.
 *
 * The two rows at the bottom are NOT the same thing, and the copy says so:
 *
 *   Unassigned    features that exist but belong to no product. The customer can
 *                 fix this by mapping a repository or picking a product.
 *   Unattributed  spend that belongs to no feature at all. Part of it is the
 *                 gap between a provider's bill and what the SDK metered, which
 *                 has no row to move, so it can never belong to a product.
 */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, type ProductSpend, type ReviewRange } from "../api";
import { money, num } from "../format";

export function ProductBreakdown({
  range,
  refreshKey = 0,
}: {
  range: ReviewRange;
  /** Bumped by the Overview's refresh control to re-pull this breakdown. */
  refreshKey?: number;
}) {
  const [data, setData] = useState<ProductSpend | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let active = true;
    setData(null);
    setFailed(false);
    api
      .productSpend(range)
      .then((d) => active && setData(d))
      .catch(() => active && setFailed(true));
    return () => {
      active = false;
    };
  }, [range, refreshKey]);

  return (
    <>
      <div className="section-head breakdown-head">
        <div>
          <h2>Cost by product</h2>
          <span className="section-sub muted">
            What each product you sell cost to build and to run, over this period.
          </span>
        </div>
      </div>

      {failed ? (
        <p className="muted">Couldn't load product spend.</p>
      ) : data === null ? (
        <p className="muted">Loading…</p>
      ) : data.products.length === 0 ? (
        <NoProducts />
      ) : (
        <section className="detail-section">
          <table className="features-table">
            <thead>
              <tr>
                <th>Product</th>
                <th className="num">Build cost</th>
                <th className="num">{data.months > 1 ? "Inference" : "Inference / mo"}</th>
                <th className="num">Features</th>
                <th>Repositories</th>
              </tr>
            </thead>
            <tbody>
              {data.products.map((p) => (
                <tr key={p.product_id}>
                  <td>
                    <Link to="/products" className="link">
                      {p.name}
                    </Link>
                  </td>
                  <td className="num">{money(p.build_cost)}</td>
                  <td className="num">{money(p.inference_cost)}</td>
                  <td className="num">{num(p.feature_count)}</td>
                  <td className="muted">
                    {p.repos.length ? p.repos.join(", ") : "no repositories mapped"}
                  </td>
                </tr>
              ))}
              <tr className="unattributed-row">
                <td>Unassigned</td>
                <td className="num">{money(data.unassigned.build_cost)}</td>
                <td className="num">{money(data.unassigned.inference_cost)}</td>
                <td className="num">{num(data.unassigned.feature_count)}</td>
                <td className="muted">
                  <Link to="/products" className="link">
                    features with no product
                  </Link>
                  {data.unassigned.spanning_count > 0 &&
                    ` — ${data.unassigned.spanning_count} span more than one`}
                </td>
              </tr>
              <tr className="unattributed-row">
                <td>Unattributed</td>
                <td className="num">{money(data.unattributed.build_cost)}</td>
                <td className="num">{money(data.unattributed.inference_cost)}</td>
                <td className="num">—</td>
                <td className="muted">spend not yet mapped to a feature</td>
              </tr>
            </tbody>
          </table>
          <p className="muted legend">
            Unassigned is spend on features that have no product yet — map a repository and it
            moves. Unattributed is spend Meter cannot tie to any feature, including the difference
            between a provider's bill and what your SDK metered, so it never belongs to a product.
          </p>
        </section>
      )}
    </>
  );
}

/** No products yet: say what one is and where to make them. */
function NoProducts() {
  return (
    <div className="empty-state">
      <p className="empty-title">No products yet</p>
      <p className="muted">
        A product is a thing you sell. Meter groups your features into products so you can see what
        each one costs to build and to run — useful when one GitHub organization holds several
        products.
      </p>
      <p className="muted">
        <Link to="/products" className="link">
          Set up products
        </Link>{" "}
        — name them, tick which repositories each is built in, and Meter assigns your features from
        the repositories their pull requests came from.
      </p>
    </div>
  );
}
