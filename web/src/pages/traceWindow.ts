/**
 * The rolling time window the request-level pages are scoped to.
 *
 * Shared by Applications and Traces so the two agree: a link that carries
 * `?days=7` means the same seven days on either page, and drilling from one to
 * the other does not silently change the period under the reader.
 *
 * A small set of presets rather than a date picker, mirroring the review-period
 * control on the feature pages. Kept in the URL so a filtered view is a link
 * someone can send.
 */
export const TRACE_WINDOWS = [
  { days: 7, label: "Last 7 days" },
  { days: 30, label: "Last 30 days" },
  { days: 90, label: "Last 90 days" },
];

export const DEFAULT_WINDOW_DAYS = 30;

/** The window named in the URL, or the default when it names one we do not offer. */
export function daysFromParams(sp: URLSearchParams): number {
  const raw = Number(sp.get("days"));
  return TRACE_WINDOWS.some((w) => w.days === raw) ? raw : DEFAULT_WINDOW_DAYS;
}
