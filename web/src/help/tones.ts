/**
 * Each chapter's tone, from the app's data palette (see chapters.tsx). Kept
 * apart from the components so the page can read a tone without importing them.
 */
export type Tone = "lime" | "teal" | "gold" | "lavender" | "coral" | "ink";

const TONES: Record<string, Tone> = {
  "getting-started": "lime",
  concepts: "lavender",
  "cost-sources": "teal",
  features: "gold",
  products: "coral",
  sdk: "ink",
  dashboards: "lime",
  optimize: "teal",
  reconciliation: "gold",
  alerts: "coral",
  trust: "lavender",
  troubleshooting: "ink",
};

export function chapterTone(slug: string): Tone {
  return TONES[slug] ?? "ink";
}
