const USD = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 0,
});

const USD_CENTS = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 2,
});

/** Format a dollar amount; whole dollars, or cents when small. `null` -> em dash. */
export function money(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return Math.abs(value) < 100 ? USD_CENTS.format(value) : USD.format(value);
}

export function num(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return new Intl.NumberFormat("en-US").format(value);
}

/** Whole-dollar amount, never cents — for compact axis/bar labels. `null` -> em dash. */
export function wholeMoney(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return USD.format(value);
}

const USD_UNIT = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 4,
});

/** Per-unit price (cost per request, per call). Sub-dollar values keep four
 *  decimals — $0.0042 rather than the $0.00 `money` would round it to, and
 *  $0.0556 rather than a $0.06 that hides the difference between two rows. */
export function unitMoney(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return Math.abs(value) < 1 ? USD_UNIT.format(value) : money(value);
}

const COMPACT_USD = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  notation: "compact",
  maximumFractionDigits: 1,
});

/** Compact dollar amount for headline figures — 58366 -> "$58.4K". `null` -> em
 *  dash. Use `money` anywhere the exact figure is the point. */
export function compactMoney(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return COMPACT_USD.format(value);
}

const COMPACT = new Intl.NumberFormat("en-US", {
  notation: "compact",
  maximumFractionDigits: 1,
});

/** Compact count, e.g. 320,000 -> "320K". `null` -> em dash (unknown). */
export function compact(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return COMPACT.format(value);
}

/** Short local date-time, e.g. "Jul 19, 3:42 PM" — used in the admin portal. */
export function shortDate(iso: string): string {
  return new Date(iso).toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

/** "claude_code" -> "Claude Code". A display label for coding-tool ids. */
export function prettyTool(tool: string): string {
  return tool
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

/** A duration a person can read at a glance. */
export function duration(ms: number | null): string {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  const minutes = Math.floor(ms / 60_000);
  return `${minutes}m ${Math.round((ms % 60_000) / 1000)}s`;
}

export function sinceNow(iso: string): string {
  const ms = Date.now() - new Date(iso).getTime();
  if (ms < 0 || Number.isNaN(ms)) return "just now";
  if (ms < 60_000) return `${Math.round(ms / 1000)}s ago`;
  if (ms < 3_600_000) return `${Math.round(ms / 60_000)}m ago`;
  if (ms < 86_400_000) return `${Math.round(ms / 3_600_000)}h ago`;
  return `${Math.round(ms / 86_400_000)}d ago`;
}
