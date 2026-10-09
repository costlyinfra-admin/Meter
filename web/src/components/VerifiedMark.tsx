/**
 * "Verified" with a tick that draws itself — the one moment in the Prove loop
 * worth marking: a saving has held for two periods and the bill agrees.
 *
 * Drawn, not bounced or burst: a finance screen marks an achievement the way
 * a reviewer ticks a box. It plays once, when the mark first appears.
 */
export function VerifiedMark() {
  return (
    <span className="opt-verified">
      <svg viewBox="0 0 20 20" width="13" height="13" aria-hidden className="verified-tick">
        <path d="m4.5 10.5 3.5 3.5 7.5-8" pathLength={1} />
      </svg>
      Verified
    </span>
  );
}
