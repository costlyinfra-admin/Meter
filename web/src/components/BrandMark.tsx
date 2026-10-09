/**
 * The CostlyInfra mark — the same logo the marketing site uses, so the app and
 * costlyinfra.com carry one identity.
 *
 * Taken from the site's own inline SVG: a rounded near-black tile, a "C" arc in
 * the page cream, and three descending lime bars (the falling-cost motif). The
 * two dark stops and the arc colour are the site's literals resolved to hex;
 * the bars use the app's --lime token, which is the same #ddf859.
 *
 * Sized in em so it scales with whichever wordmark it sits beside — the
 * sidebar's and the larger one on the auth cards — exactly as the CSS mark it
 * replaces did.
 */
import { useEffect, useId, useState } from "react";
import { markSeen, seen } from "../once";

/** The bars settle once per visit — on the first screen that shows the mark. */
const SETTLED = "meter.motion.brand";

export function BrandMark({ className }: { className?: string }) {
  // Gradient ids must be unique per instance: two marks on one page (shell +
  // a dialog, say) would otherwise both reference whichever rendered first.
  const id = useId();
  const tile = `brand-tile-${id}`;
  const gloss = `brand-gloss-${id}`;
  // Decided on first render, recorded after: the three bars drop into place
  // (the falling-cost motif, literally) the first time a visit shows the mark.
  const [settle] = useState(() => !seen(SETTLED, "session"));
  useEffect(() => {
    if (settle) markSeen(SETTLED, "session");
  }, [settle]);

  return (
    <svg
      viewBox="0 0 48 48"
      className={`${className ?? "brand-mark"}${settle ? " settle" : ""}`}
      role="img"
      aria-hidden="true"
      focusable="false"
    >
      <defs>
        <linearGradient id={tile} x1="6" y1="4" x2="42" y2="46" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="#2f322a" />
          <stop offset="1" stopColor="#151612" />
        </linearGradient>
        <linearGradient id={gloss} x1="24" y1="0" x2="24" y2="48" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="#ffffff" stopOpacity="0.3" />
          <stop offset="0.5" stopColor="#ffffff" stopOpacity="0" />
        </linearGradient>
      </defs>
      <rect x="2" y="2" width="44" height="44" rx="13" fill={`url(#${tile})`} />
      <rect x="2" y="2" width="44" height="44" rx="13" fill={`url(#${gloss})`} />
      <rect
        x="2"
        y="2"
        width="44"
        height="44"
        rx="13"
        fill="none"
        stroke="#ffffff"
        strokeOpacity="0.22"
        strokeWidth="1"
      />
      <path
        d="M32.5 16.5c-1.8-2.6-5-4.2-8.5-4.2-5.8 0-10.5 4.7-10.5 11.7s4.7 11.7 10.5 11.7c3.5 0 6.7-1.6 8.5-4.2"
        fill="none"
        stroke="#faf9f5"
        strokeWidth="4.4"
        strokeLinecap="round"
      />
      <rect
        className="brand-bar"
        style={{ animationDelay: "120ms" }}
        x="26.5"
        y="20"
        width="3.4"
        height="9"
        rx="1.7"
        fill="var(--lime)"
      />
      <rect
        className="brand-bar"
        style={{ animationDelay: "220ms" }}
        x="31.5"
        y="23.5"
        width="3.4"
        height="5.5"
        rx="1.7"
        fill="var(--lime)"
        fillOpacity="0.8"
      />
      <rect
        className="brand-bar"
        style={{ animationDelay: "320ms" }}
        x="36.5"
        y="26.5"
        width="3.4"
        height="2.5"
        rx="1.25"
        fill="var(--lime)"
        fillOpacity="0.6"
      />
    </svg>
  );
}
