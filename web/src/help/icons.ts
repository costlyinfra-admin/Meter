import type { IconName } from "../components/LineIcon";

/** Each category's mark, from the same family as the navigation — the nav's
 *  own icon wherever a category is about one of its pages, so the handbook and
 *  the product point at the same thing the same way. */
export const CATEGORY_ICON: Record<string, IconName> = {
  "getting-started": "compass",
  concepts: "idea",
  "cost-sources": "sources",
  features: "features",
  products: "products",
  sdk: "sdk",
  dashboards: "overview",
  optimize: "optimize",
  reconciliation: "reconciliation",
  alerts: "alerts",
  trust: "shield",
  troubleshooting: "lifebuoy",
};
