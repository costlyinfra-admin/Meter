/** Shared badges for confidence, the "Worth it?" indicator, and AI/non-AI kind. */

export function ConfidenceBadge({ level }: { level: string | null }) {
  const l = level ?? "low";
  return <span className={`badge conf-${l}`}>{l}</span>;
}

const WORTH_LABEL: Record<string, string> = {
  healthy: "Healthy",
  watch: "Watch",
  unknown: "No usage",
};

export function WorthBadge({ value }: { value: string }) {
  return <span className={`badge worth-${value}`}>{WORTH_LABEL[value] ?? value}</span>;
}

/** The product-surface vocabulary, mirroring discovery.CATEGORY_LABELS. The
 *  server publishes the same list at /api/features/categories for the picker;
 *  this map only renders what a row already carries, so an unknown value still
 *  displays rather than disappearing. */
const CATEGORY_LABELS: Record<string, string> = {
  chat: "Chat",
  api: "API",
  ui: "UI",
  docs: "Docs",
  data: "Data/ETL",
  auth: "Auth",
  reporting: "Reporting",
  integration: "Integration",
  infra: "Infra",
};

/** Where a category came from — on hover, since a guess and a human tag are
 *  not the same claim. */
const CATEGORY_BASIS: Record<string, string> = {
  user: "Tagged by hand",
  discovery: "Guessed from keywords in its pull requests",
};

/** Where a product assignment came from — a person, or the repo mapping. The
 *  two are not the same claim, exactly as with a feature's Type. */
const PRODUCT_BASIS: Record<string, string> = {
  user: "Assigned by hand",
  discovery: "Derived from the repositories its pull requests are in",
};

export function ProductBadge({
  product,
  source,
}: {
  product: string | null;
  source?: string | null;
}) {
  if (!product) {
    return (
      <span className="badge cat-untagged" title="This feature is not part of any product yet">
        Unassigned
      </span>
    );
  }
  return (
    <span className="badge" title={source ? PRODUCT_BASIS[source] : undefined}>
      {product}
    </span>
  );
}

export function CategoryBadge({
  category,
  source,
}: {
  category: string | null;
  source?: string | null;
}) {
  if (!category) {
    return (
      <span className="badge cat-untagged" title="Nobody has tagged this feature yet">
        Untagged
      </span>
    );
  }
  return (
    <span className={`badge cat-${category}`} title={source ? CATEGORY_BASIS[source] : undefined}>
      {CATEGORY_LABELS[category] ?? category}
    </span>
  );
}
