/**
 * A short confirmation that an action worked — "GitHub connected", "Alert
 * saved" — shown in a corner for a few seconds by the Toaster in AppShell.
 *
 * For actions whose result is otherwise easy to miss: ones that move you to
 * another page, or change a row somewhere else on this one. Never for
 * failures: an error stays on the screen, next to what failed, until it is
 * dealt with.
 *
 * An event on window rather than a context, so anything can confirm without
 * being wired to the shell — and a page with no shell around it (a test, say)
 * simply shows nothing.
 */
export const TOAST_EVENT = "meter:toast";

export function toast(message: string): void {
  window.dispatchEvent(new CustomEvent<string>(TOAST_EVENT, { detail: message }));
}
