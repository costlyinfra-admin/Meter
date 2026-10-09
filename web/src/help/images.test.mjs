/**
 * The knowledge base's screenshots, held against the files on disk.
 *
 * Plain JS rather than TypeScript, as styles.test.mjs is and for the same
 * reason: it reads the filesystem, and app code has no Node types.
 */
import { readdirSync, statSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { CATEGORIES } from "./content.ts";

const DIR = resolve(process.cwd(), "public/kb");
const images = CATEGORIES.flatMap((c) =>
  c.topics.flatMap((t) =>
    t.blocks.filter((b) => b.kind === "image").map((b) => ({ ...b, at: `${c.slug}/${t.slug}` })),
  ),
);

describe("knowledge base images", () => {
  it("every image a topic shows is on disk", () => {
    const files = new Set(readdirSync(DIR));
    for (const img of images) expect(files, `${img.at} → ${img.file}`).toContain(img.file);
  });

  it("no image on disk is left unused", () => {
    const used = new Set(images.map((img) => img.file));
    for (const file of readdirSync(DIR)) expect(used, file).toContain(file);
  });

  it("every image says what it shows", () => {
    for (const img of images) expect(img.alt.length, img.at).toBeGreaterThan(20);
  });

  it("keeps each image small enough to load quickly", () => {
    for (const file of readdirSync(DIR)) {
      expect(statSync(resolve(DIR, file)).size, file).toBeLessThan(300 * 1024);
    }
  });
});
