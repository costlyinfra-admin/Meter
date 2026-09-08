/**
 * A code block with a copy button.
 *
 * Every snippet in this app exists to be copied — an ingest token, an install
 * command, a wrapped client, a whole prompt for a coding agent. Selecting
 * multi-line text out of a scrolling `<pre>` is exactly the kind of small
 * friction that gets setup abandoned halfway through.
 *
 * The button reports what happened rather than assuming: the Clipboard API is
 * unavailable over plain HTTP and can be refused by permissions policy, and a
 * button that silently did nothing is worse than one that says so.
 */
import { useEffect, useRef, useState } from "react";

type State = "idle" | "copied" | "failed";

function CopyIcon() {
  return (
    <svg
      viewBox="0 0 20 20"
      width="13"
      height="13"
      aria-hidden
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <rect x="7" y="7" width="9.5" height="9.5" rx="1.6" />
      <path d="M13 4.5A1.5 1.5 0 0 0 11.5 3h-6A2.5 2.5 0 0 0 3 5.5v6A1.5 1.5 0 0 0 4.5 13" />
    </svg>
  );
}

export function Snippet({
  children,
  className = "",
  copyLabel = "Copy",
  sensitive = false,
}: {
  /** The snippet's text — what is shown, and what is copied. */
  children: string;
  className?: string;
  /** Say what is being copied when the block is one of several on a page. */
  copyLabel?: string;
  /**
   * This block renders a live secret. Session replay records the DOM, and the
   * "mask user input" default only covers form fields — text like an ingest
   * token would be captured verbatim. Marking the element masks its whole
   * subtree, whatever the default is set to.
   */
  sensitive?: boolean;
}) {
  const [state, setState] = useState<State>("idle");
  const timer = useRef<ReturnType<typeof setTimeout>>();

  // The button can be clicked and the page navigated away from before this
  // fires, so the timeout is cleared rather than left to land on nothing.
  useEffect(() => () => clearTimeout(timer.current), []);

  async function copy() {
    clearTimeout(timer.current);
    try {
      await navigator.clipboard.writeText(children);
      setState("copied");
    } catch {
      setState("failed");
    }
    timer.current = setTimeout(() => setState("idle"), 2000);
  }

  return (
    <div className="snippet-wrap" data-dd-privacy={sensitive ? "mask" : undefined}>
      <pre className={className ? `snippet ${className}` : "snippet"}>{children}</pre>
      <button
        type="button"
        className="snippet-copy"
        onClick={copy}
        // The accessible name follows the state, so a click gives the same
        // feedback whether you can see the button or are hearing it.
        aria-label={
          state === "copied"
            ? "Copied"
            : state === "failed"
              ? "Copying failed — select and copy manually"
              : copyLabel
        }
      >
        {state === "idle" && <CopyIcon />}
        {state === "copied" ? "Copied" : state === "failed" ? "Select and copy" : copyLabel}
      </button>
    </div>
  );
}
