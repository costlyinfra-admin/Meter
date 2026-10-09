/**
 * The content model for the knowledge base.
 *
 * Content is data (see content.ts), not markup or markdown, for three reasons:
 * it needs no parser dependency, it is type-checked like the rest of the app, and
 * a topic can link straight into the product — "open [Connect sources](/cost-sources)"
 * is a real router link, not a dead string.
 *
 * Rendering lives in render.tsx; this file is data and helpers only, so content
 * can be imported without pulling in React.
 */
export type Block =
  | { kind: "p"; text: string }
  | { kind: "list"; items: string[] }
  | { kind: "steps"; items: string[] }
  | { kind: "code"; text: string }
  | { kind: "note"; text: string }
  | { kind: "table"; head: string[]; rows: string[][] }
  /** A screenshot of Meter. `file` is a name in web/public/kb/ — served at
   *  /kb/<file> in the app and copied beside the public docs — so content never
   *  holds a URL that differs between the two. `alt` says what the picture
   *  shows for someone who cannot see it; `caption` is optional prose under it. */
  | { kind: "image"; file: string; alt: string; caption?: string };

// Readable shorthands for the content file.
export const p = (text: string): Block => ({ kind: "p", text });
export const list = (...items: string[]): Block => ({ kind: "list", items });
export const steps = (...items: string[]): Block => ({ kind: "steps", items });
export const code = (text: string): Block => ({ kind: "code", text });
export const note = (text: string): Block => ({ kind: "note", text });
export const table = (head: string[], rows: string[][]): Block => ({ kind: "table", head, rows });
export const image = (file: string, alt: string, caption?: string): Block => ({
  kind: "image",
  file,
  alt,
  caption,
});

/** Where a knowledge-base image is served in the app. */
export const KB_IMAGE_DIR = "/kb";

/** Drop the inline syntax, leaving the prose a reader would see.
 *
 * Search indexes and excerpts this, not the raw text: someone searching for a
 * phrase should match it whether or not it happens to be bold, and a result
 * snippet should never show them `**` or a markdown link. */
export function stripInline(text: string): string {
  return text
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/`([^`]+)`/g, "$1");
}

/** Plain text of a block, for the search index and its excerpts. */
export function blockText(block: Block): string {
  switch (block.kind) {
    case "p":
    case "code":
    case "note":
      return stripInline(block.text);
    case "list":
    case "steps":
      return block.items.map(stripInline).join(" ");
    case "table":
      return [...block.head, ...block.rows.flat()].map(stripInline).join(" ");
    case "image":
      // What the picture shows is searchable, the same words a screen reader
      // hears; the file name is not.
      return [block.alt, block.caption ?? ""].map(stripInline).join(" ").trim();
  }
}
