/**
 * Light/dark theme.
 *
 * The stored preference has three states — "light", "dark", or nothing at all,
 * which means follow the operating system. What lands on the document is always
 * a *resolved* theme, "light" or "dark", so the stylesheet needs one selector
 * (`[data-theme="dark"]`) rather than one for the explicit choice and another
 * for the system default.
 *
 * The first paint is handled by an inline script in index.html, not here: a
 * theme applied after React mounts is a white flash on a dark-mode machine.
 * This module owns everything after that, and the two must agree on the key.
 */
export type Theme = "light" | "dark";

export const STORAGE_KEY = "meter.theme";

const DARK = "(prefers-color-scheme: dark)";

function media(): MediaQueryList | null {
  return typeof window.matchMedia === "function" ? window.matchMedia(DARK) : null;
}

/** The user's explicit choice, or null when they have not made one. */
export function stored(): Theme | null {
  try {
    const value = localStorage.getItem(STORAGE_KEY);
    return value === "light" || value === "dark" ? value : null;
  } catch {
    // Private mode, or storage blocked: no preference, follow the system.
    return null;
  }
}

export function systemTheme(): Theme {
  return media()?.matches ? "dark" : "light";
}

/** What should actually be on screen: the choice if there is one, else the OS. */
export function resolved(): Theme {
  return stored() ?? systemTheme();
}

export function apply(theme: Theme): void {
  document.documentElement.setAttribute("data-theme", theme);
}

/** Record a choice and show it. Passing null hands control back to the OS. */
export function choose(theme: Theme | null): Theme {
  try {
    if (theme) localStorage.setItem(STORAGE_KEY, theme);
    else localStorage.removeItem(STORAGE_KEY);
  } catch {
    // The theme still applies for this session; it just won't be remembered.
  }
  const next = theme ?? systemTheme();
  apply(next);
  return next;
}

/**
 * Follow the operating system while the user has made no choice of their own.
 *
 * Someone whose machine switches at sunset expects the app to switch with it.
 * Someone who has picked a theme has said what they want, and a sunset is not a
 * reason to overrule them.
 */
export function watchSystem(onChange: (theme: Theme) => void): () => void {
  const query = media();
  if (!query) return () => {};
  const handler = () => {
    if (stored()) return;
    const next = systemTheme();
    apply(next);
    onChange(next);
  };
  query.addEventListener("change", handler);
  return () => query.removeEventListener("change", handler);
}
