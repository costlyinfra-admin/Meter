/**
 * Where confirmations from toast() appear: top-right, one under another, each
 * gone after a few seconds. Top-right because the bottom-right is Ask Meter's.
 *
 * A polite live region, so a screen reader hears "Alert saved" without being
 * interrupted mid-sentence.
 */
import { useEffect, useState } from "react";
import { TOAST_EVENT } from "../toast";

/** How long a confirmation stays. Long enough to read twice. */
export const TOAST_MS = 4000;
/** How long it takes to leave, which the stylesheet's .leaving matches. */
const LEAVE_MS = 200;

interface Item {
  id: number;
  message: string;
  leaving: boolean;
}

let nextId = 1;

export function Toaster() {
  const [items, setItems] = useState<Item[]>([]);

  useEffect(() => {
    const timers: number[] = [];
    function onToast(event: Event) {
      const message = (event as CustomEvent<string>).detail;
      if (!message) return;
      const id = nextId++;
      // The same confirmation twice in a row is one confirmation.
      setItems((now) => [
        ...now.filter((t) => t.message !== message),
        { id, message, leaving: false },
      ]);
      timers.push(
        window.setTimeout(
          () => setItems((now) => now.map((t) => (t.id === id ? { ...t, leaving: true } : t))),
          TOAST_MS,
        ),
        window.setTimeout(
          () => setItems((now) => now.filter((t) => t.id !== id)),
          TOAST_MS + LEAVE_MS,
        ),
      );
    }
    window.addEventListener(TOAST_EVENT, onToast);
    return () => {
      window.removeEventListener(TOAST_EVENT, onToast);
      timers.forEach((t) => window.clearTimeout(t));
    };
  }, []);

  return (
    <div className="toaster" role="status" aria-live="polite">
      {items.map((t) => (
        <p key={t.id} className={t.leaving ? "toast leaving" : "toast"}>
          <svg viewBox="0 0 20 20" width="15" height="15" aria-hidden className="toast-mark">
            <path d="m5.5 10.5 3 3 6-7" />
          </svg>
          {t.message}
        </p>
      ))}
    </div>
  );
}
