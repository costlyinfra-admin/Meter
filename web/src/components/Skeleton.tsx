/**
 * A loading placeholder shaped like what is coming: soft grey blocks that
 * gently pulse, where a panel used to say "Loading…".
 *
 * The words are still there for anyone who cannot see the shapes — a screen
 * reader reads "Loading…" (or the label given), as it read the text this
 * replaces — so this changes how waiting looks, not what it says.
 */
export function Skeleton({
  variant = "lines",
  label = "Loading…",
}: {
  /** "lines": a few lines of text. "chart": a chart frame with lines beside
   *  it. "page": a row of cards above a few panels, for a whole page. */
  variant?: "lines" | "chart" | "page";
  label?: string;
}) {
  return (
    // Busy, not a status: a page's own status message (a test's outcome, a
    // sync result) is what role="status" should find once this is gone.
    <div className={`skeleton skeleton-${variant}`} aria-busy="true">
      <span className="sr-only">{label}</span>
      {variant === "page" && (
        <>
          <div className="skeleton-row" aria-hidden>
            {[0, 1, 2, 3].map((i) => (
              <span key={i} className="skeleton-block skeleton-card" />
            ))}
          </div>
          <div className="skeleton-row" aria-hidden>
            {[0, 1, 2].map((i) => (
              <span key={i} className="skeleton-block skeleton-panel" />
            ))}
          </div>
        </>
      )}
      {variant === "chart" && (
        <div className="skeleton-row" aria-hidden>
          <span className="skeleton-block skeleton-chart" />
          <span className="skeleton-stack">
            <span className="skeleton-block skeleton-line" />
            <span className="skeleton-block skeleton-line" />
            <span className="skeleton-block skeleton-line short" />
          </span>
        </div>
      )}
      {variant === "lines" && (
        <span className="skeleton-stack" aria-hidden>
          <span className="skeleton-block skeleton-line" />
          <span className="skeleton-block skeleton-line" />
          <span className="skeleton-block skeleton-line short" />
        </span>
      )}
    </div>
  );
}
