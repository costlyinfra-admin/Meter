/**
 * "Which product is this feature part of?" — shown beside the feature's Type.
 *
 * The vocabulary is the customer's own product list, so unlike CategoryPicker
 * it is NOT cached at module level: a product added on the Products page has to
 * appear here immediately. The list is passed in rather than fetched per row,
 * because a feature table renders twenty of these at once.
 *
 * Choosing one records a USER assignment, which no discovery run and no
 * re-application of the repo mapping will overwrite. Choosing "Unassigned"
 * clears it and hands the feature back to the mapping.
 */
import type { Product } from "../api";

export function ProductPicker({
  value,
  products,
  onChange,
}: {
  value: string | null;
  products: Product[] | null;
  onChange: (productId: string | null) => Promise<void> | void;
}) {
  return (
    <select
      className="category-picker"
      aria-label="Product"
      title="Which product this feature is part of. Your choice is kept — re-running discovery or re-applying the repo mapping won't change it."
      value={value ?? ""}
      disabled={products === null}
      onChange={(e) => onChange(e.target.value || null)}
    >
      <option value="">Unassigned</option>
      {(products ?? []).map((p) => (
        <option key={p.id} value={p.id}>
          {p.name}
        </option>
      ))}
    </select>
  );
}
