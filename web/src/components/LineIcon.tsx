/**
 * One family of line icons, all drawn on the same 20x20 grid at the same
 * weight, so wherever they appear — the navigation, the knowledge base — they
 * read as a set rather than as borrowed glyphs. Colour comes from the text
 * around them (currentColor).
 */

export type IconName =
  | "overview"
  | "sources"
  | "applications"
  | "features"
  | "traces"
  | "optimize"
  | "alerts"
  | "sdk"
  | "settings"
  | "help"
  | "pricing"
  | "forecast"
  | "prompts"
  | "products"
  | "reconciliation"
  | "compass"
  | "idea"
  | "shield"
  | "lifebuoy";

export function LineIcon({
  name,
  size = 17,
  className,
}: {
  name: IconName;
  size?: number;
  className?: string;
}) {
  return (
    <svg
      viewBox="0 0 20 20"
      width={size}
      height={size}
      aria-hidden
      className={className}
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      {name === "overview" && (
        <>
          <rect x="2.75" y="2.75" width="6" height="6" rx="1.5" />
          <rect x="11.25" y="2.75" width="6" height="6" rx="1.5" />
          <rect x="2.75" y="11.25" width="6" height="6" rx="1.5" />
          <rect x="11.25" y="11.25" width="6" height="6" rx="1.5" />
        </>
      )}
      {name === "sources" && (
        <>
          <path d="M10 2.5 17.5 6.5 10 10.5 2.5 6.5Z" />
          <path d="M2.5 10 10 14l7.5-4" />
          <path d="M2.5 13.5 10 17.5l7.5-4" />
        </>
      )}
      {/* Applications: stacked surfaces, the layer above a feature. */}
      {name === "applications" && (
        <>
          <rect x="2.75" y="2.75" width="6" height="6" rx="1.5" />
          <rect x="11.25" y="2.75" width="6" height="6" rx="1.5" />
          <rect x="2.75" y="11.25" width="6" height="6" rx="1.5" />
          <rect x="11.25" y="11.25" width="6" height="6" rx="1.5" />
        </>
      )}
      {/* Traces: a waterfall of spans, which is what the page draws. */}
      {name === "traces" && (
        <>
          <path d="M3 5h9" />
          <path d="M6 10h8" />
          <path d="M9 15h5" />
        </>
      )}
      {name === "features" && (
        <>
          <path d="M9.4 2.75H16A1.25 1.25 0 0 1 17.25 4v6.6c0 .33-.13.65-.37.89l-5.4 5.4a1.25 1.25 0 0 1-1.76 0l-6.6-6.6a1.25 1.25 0 0 1 0-1.76l5.4-5.4c.23-.24.55-.38.88-.38Z" />
          <circle cx="13.35" cy="6.65" r="1.1" />
        </>
      )}
      {name === "optimize" && (
        <>
          <path d="M2.75 5.5 7.5 10.25 11 6.75l6.25 6.25" />
          <path d="M13 13h4.25V8.75" />
        </>
      )}
      {name === "products" && (
        <>
          <path d="M10 2.75 3.25 6.1v7.8L10 17.25l6.75-3.35V6.1Z" />
          <path d="M3.25 6.1 10 9.45l6.75-3.35M10 9.45v7.8" />
        </>
      )}
      {name === "prompts" && (
        <>
          <path d="M4.75 3.75h10.5a2 2 0 0 1 2 2v5.25a2 2 0 0 1-2 2H9.4l-3.65 2.9v-2.9H4.75a2 2 0 0 1-2-2V5.75a2 2 0 0 1 2-2Z" />
          <path d="M6.4 7.1h7.2M6.4 9.9h4.4" />
        </>
      )}
      {name === "alerts" && (
        <>
          <path d="M6 8.25a4 4 0 0 1 8 0c0 3.4 1.1 4.4 1.45 4.9a.4.4 0 0 1-.33.6H4.88a.4.4 0 0 1-.33-.6c.35-.5 1.45-1.5 1.45-4.9Z" />
          <path d="M8.6 16.1a1.9 1.9 0 0 0 2.8 0" />
        </>
      )}
      {name === "sdk" && (
        <>
          <path d="M7.25 6.5 3.75 10l3.5 3.5" />
          <path d="M12.75 6.5 16.25 10l-3.5 3.5" />
        </>
      )}
      {name === "settings" && (
        <>
          <path d="M2.75 6.75h3.5M10.75 6.75h6.5" />
          <circle cx="8.5" cy="6.75" r="1.6" />
          <path d="M2.75 13.25h7.5M14.75 13.25h2.5" />
          <circle cx="12.5" cy="13.25" r="1.6" />
        </>
      )}
      {name === "reconciliation" && (
        <>
          <path d="M4.5 2.75h7.2l3.8 3.8v10.7a.75.75 0 0 1-.75.75H4.5a.75.75 0 0 1-.75-.75V3.5a.75.75 0 0 1 .75-.75Z" />
          <path d="M11.5 2.9v3.9h3.9" />
          <path d="m6.6 12.4 1.8 1.8 3.5-3.5" />
        </>
      )}
      {name === "forecast" && (
        <>
          <path d="M3 15.5 7.5 11l3 3L17 7.5" />
          <path d="M13 7.5h4v4" strokeDasharray="1.6 1.6" />
        </>
      )}
      {name === "pricing" && (
        <>
          <path d="M3.4 3.4h5.1l8.1 8.1-5.1 5.1-8.1-8.1V3.4Z" />
          <circle cx="6.6" cy="6.6" r="1.1" />
        </>
      )}
      {name === "help" && (
        <>
          <path d="M10 5.6S8.4 3.4 3 3.4v10.9c5.4 0 7 2.2 7 2.2s1.6-2.2 7-2.2V3.4c-5.4 0-7 2.2-7 2.2Z" />
          <path d="M10 5.6v10.9" />
        </>
      )}
      {/* The knowledge base's categories that have no page of their own. */}
      {name === "compass" && (
        <>
          <circle cx="10" cy="10" r="7.25" />
          <path d="m12.9 7.1-1.75 4.05-4.05 1.75 1.75-4.05Z" />
        </>
      )}
      {name === "idea" && (
        <>
          <path d="M7.4 13.4c0-1.6-2.15-2.6-2.15-5.15a4.75 4.75 0 0 1 9.5 0c0 2.55-2.15 3.55-2.15 5.15Z" />
          <path d="M7.75 16.25h4.5" />
        </>
      )}
      {name === "shield" && (
        <>
          <path d="M10 2.75 16.25 5v4.6c0 3.7-2.6 6.3-6.25 7.65C6.35 15.9 3.75 13.3 3.75 9.6V5Z" />
          <path d="m7.4 10 1.8 1.8 3.4-3.4" />
        </>
      )}
      {name === "lifebuoy" && (
        <>
          <circle cx="10" cy="10" r="7.25" />
          <circle cx="10" cy="10" r="3" />
          <path d="m4.9 4.9 2.95 2.95M12.15 12.15l2.95 2.95M15.1 4.9l-2.95 2.95M7.85 12.15 4.9 15.1" />
        </>
      )}
    </svg>
  );
}
