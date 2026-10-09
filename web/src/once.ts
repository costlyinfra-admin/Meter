/**
 * Moments that should happen once: the logo settling on the first screen of a
 * visit, the first time every dollar is attributed.
 *
 * Remembered in the viewer's own browser — a per-viewer nicety, never state
 * anything depends on. Where storage is unavailable (a private window, blocked
 * site data) the moment is treated as already seen: the page renders exactly
 * the same, just without the flourish.
 *
 * Read and write are separate on purpose. A component decides during render
 * with `seen` (pure, so React may call it twice) and records with `markSeen`
 * after it has shown the moment, in an effect.
 */
type Where = "session" | "local";

function store(where: Where): Storage {
  // Read as globals at call time: getting at either can itself throw (blocked
  // site data), which the callers below catch.
  return where === "session" ? sessionStorage : localStorage;
}

export function seen(key: string, where: Where = "local"): boolean {
  try {
    return store(where).getItem(key) !== null;
  } catch {
    return true;
  }
}

export function markSeen(key: string, where: Where = "local"): void {
  try {
    store(where).setItem(key, "1");
  } catch {
    // Nowhere to remember it; it simply will not play.
  }
}
