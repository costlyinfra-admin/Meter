/**
 * Each chapter's mark: its icon, from the same LineIcon family as the
 * navigation (help/icons.ts), in a small tile tinted with the chapter's tone.
 *
 * The tone is decoration and wayfinding only — it never carries a meaning a
 * reader has to decode, and every chapter is also named in words beside it, so
 * the mark is hidden from screen readers.
 */
import { LineIcon } from "../components/LineIcon";
import { CATEGORY_ICON } from "./icons";
import { chapterTone } from "./tones";

const ICON_SIZE = { sm: 13, md: 17, lg: 22 } as const;

export function ChapterMark({ slug, size = "md" }: { slug: string; size?: "sm" | "md" | "lg" }) {
  return (
    <span className={`kb-mark kb-mark-${size} tone-${chapterTone(slug)}`} aria-hidden>
      <LineIcon name={CATEGORY_ICON[slug] ?? "help"} size={ICON_SIZE[size]} />
    </span>
  );
}
