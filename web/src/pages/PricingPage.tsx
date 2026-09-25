/**
 * Provider pricing — the published list rates Meter costs hook-metered traffic
 * at, shown so a customer can check the arithmetic instead of taking it on
 * faith. Every measured saving on the Recommendations screen is these rates
 * times a counted number of tokens; a price book nobody can read is the part
 * of that sentence you have to trust blindly.
 *
 * Rendered straight from `pricing.py` through /api/pricing, so the screen and
 * the costing cannot drift. Nothing here is tenant data — no spend, no usage —
 * which is why it can sit under Help rather than behind an admin gate.
 *
 * The cache columns are on this page and not a separate one because they are
 * what the prompt-caching finding is made of: a prefix below a model's minimum
 * cannot be cached at all, and a write costs MORE than not caching. Both facts
 * live here next to the rates they modify.
 */
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError, type PriceBook, type PriceBookModel } from "../api";

/** A rate per million tokens. Trailing zeros trimmed — "$3" not "$3.0000" —
 *  but never rounded away: $0.025 is a real price, not a rounding artifact. */
function rate(value: string | null): string {
  if (value === null) return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  if (n === 0) return "$0";
  // Up to four decimals, which is as fine as any published rate goes.
  return `$${n.toFixed(4).replace(/\.?0+$/, "")}`;
}

function tokens(value: number | null): string {
  return value === null ? "—" : value.toLocaleString();
}

/** How a provider's cache is turned on. Not decoration: it decides what the
 *  prompt-caching recommendation tells you to DO, and the two answers have
 *  nothing in common. */
function CacheMode({ automatic }: { automatic: boolean | null }) {
  if (automatic === null) return <span className="muted">—</span>;
  return automatic ? (
    <span className="badge" title="Cached automatically; you cannot switch it on or off">
      automatic
    </span>
  ) : (
    <span className="badge" title="You mark what to cache (cache_control)">
      opt-in
    </span>
  );
}

function ModelRow({ model }: { model: PriceBookModel }) {
  return (
    <tr>
      <th scope="row" className="price-model">
        <code>{model.model}</code>
        {model.open_weights_family && (
          <span className="section-sub muted"> {model.open_weights_family}</span>
        )}
      </th>
      <td className="num">{rate(model.input_per_million)}</td>
      <td className="num">{rate(model.output_per_million)}</td>
      <td className="num">{rate(model.cache_read_per_million)}</td>
      <td className="num">{rate(model.cache_write_per_million)}</td>
      <td className="num">{tokens(model.min_cacheable_tokens)}</td>
      <td>
        <CacheMode automatic={model.cache_is_automatic} />
      </td>
    </tr>
  );
}

export function PricingPage() {
  const [book, setBook] = useState<PriceBook | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");

  useEffect(() => {
    let live = true;
    api
      .pricing()
      .then((r) => live && setBook(r))
      .catch(
        (err) =>
          live && setError(err instanceof ApiError ? err.message : "Could not load pricing."),
      );
    return () => {
      live = false;
    };
  }, []);

  const visible = useMemo(() => {
    if (!book) return [];
    const q = query.trim().toLowerCase();
    if (!q) return book.providers;
    return book.providers
      .map((p) => ({
        ...p,
        models: p.models.filter(
          (m) => m.model.toLowerCase().includes(q) || p.label.toLowerCase().includes(q),
        ),
      }))
      .filter((p) => p.models.length > 0);
  }, [book, query]);

  const total = book?.providers.reduce((n, p) => n + p.models.length, 0) ?? 0;

  return (
    <div className="content">
      <div className="dash-head">
        <div>
          <h1>Provider pricing</h1>
          <p className="muted dash-sub">
            The published list rates Meter prices metered tokens at. Rates are per million tokens,
            standard context, before any discount you have negotiated.
          </p>
        </div>
      </div>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      {book && (
        <>
          <p className="section-sub muted">
            Price book version <strong>{book.version}</strong> · {total} models across{" "}
            {book.providers.length} providers. These are the same tables that cost your{" "}
            <Link to="/install-sdk" className="link">
              SDK-metered
            </Link>{" "}
            traffic, so what you see here is what Meter charged. Connector spend comes from the
            provider&rsquo;s own bill and is not priced from this table at all — see{" "}
            <Link to="/help/reconciliation/how-the-comparison-works" className="link">
              how the comparison works
            </Link>
            .
          </p>

          <div className="tabs" role="presentation">
            <input
              type="search"
              className="tab-search"
              placeholder="Filter models"
              aria-label="Filter models"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </div>

          {visible.length === 0 && <p className="muted">No model matches that filter.</p>}

          {visible.map((provider) => (
            <section key={provider.provider} className="detail-section">
              <div className="section-head">
                <h2>{provider.label}</h2>
                <span className="section-sub muted">
                  {provider.checked ? (
                    <>Checked against the published table on {provider.checked}</>
                  ) : (
                    // Said plainly rather than left blank: an unchecked rate is
                    // a different thing from a checked one, and the reader is
                    // the person who would notice it had gone stale.
                    <>Not reconciled against the provider&rsquo;s page recently</>
                  )}
                  {provider.source_url && (
                    <>
                      {" · "}
                      <a
                        href={provider.source_url}
                        target="_blank"
                        rel="noreferrer noopener"
                        className="link"
                      >
                        source
                      </a>
                    </>
                  )}
                </span>
              </div>
              {/* Seven columns will not fit a phone. Scroll the TABLE rather
                  than the page: a layout that slides sideways under your thumb
                  makes every other screen feel broken too. */}
              <div className="price-table-wrap">
                <table className="mini-table price-table">
                  <thead>
                    <tr>
                      <th scope="col">Model</th>
                      <th scope="col" className="num">
                        Input
                      </th>
                      <th scope="col" className="num">
                        Output
                      </th>
                      <th scope="col" className="num" title="Reading a prefix back from the cache">
                        Cache read
                      </th>
                      <th
                        scope="col"
                        className="num"
                        title="Writing a prefix into the cache — more than sending it uncached"
                      >
                        Cache write
                      </th>
                      <th
                        scope="col"
                        className="num"
                        title="Smallest prefix this model will cache at all"
                      >
                        Min cacheable
                      </th>
                      <th scope="col">Caching</th>
                    </tr>
                  </thead>
                  <tbody>
                    {provider.models.map((m) => (
                      <ModelRow key={m.model} model={m} />
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          ))}

          <p className="section-sub muted">
            A dash means Meter does not price that column for the model, which is not the same as it
            being free. Cache write is the 5-minute rate where a provider charges one. Why the write
            side matters at all:{" "}
            <Link to="/help/optimize/prompt-caching" className="link">
              prompt caching
            </Link>
            .
          </p>
        </>
      )}
    </div>
  );
}
