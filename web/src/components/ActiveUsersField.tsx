/**
 * "How many people actually use this feature?" — typed in, because nothing
 * tells us.
 *
 * Cost per user and the "Worth it?" indicator are both derived from this
 * number, and until product-analytics connectors land (design doc §11, Slice 2)
 * there is no connector that can supply it. Without an entry path those two
 * columns can never fill, which is how they sat blank for every real tenant
 * while the demo seed made them look implemented.
 *
 * Active users is a point-in-time count rather than a running total, so it
 * belongs to one month — and this says which month it is writing, because the
 * page is showing a range and the figure is read from that range's last month.
 */
import { useEffect, useRef, useState } from "react";
import { num } from "../format";

function monthLabel(month: string): string {
  const [year, m] = month.split("-").map(Number);
  if (!year || !m) return month;
  return new Date(Date.UTC(year, m - 1, 1)).toLocaleDateString(undefined, {
    month: "long",
    year: "numeric",
    timeZone: "UTC",
  });
}

export function ActiveUsersField({
  value,
  month,
  onSave,
}: {
  value: number | null;
  /** YYYY-MM — the month this figure belongs to. */
  month: string;
  onSave: (activeUsers: number) => Promise<void>;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (editing) inputRef.current?.focus();
  }, [editing]);

  const label = monthLabel(month);

  if (!editing) {
    return (
      <span className="active-users-field">
        {value != null ? (
          <span className="muted">
            {num(value)} active users in {label}
          </span>
        ) : (
          <span className="muted">No active users recorded for {label}</span>
        )}
        <button
          type="button"
          className="link active-users-edit"
          onClick={() => {
            setDraft(value != null ? String(value) : "");
            setError(null);
            setEditing(true);
          }}
        >
          {value != null ? "Edit" : `Set active users`}
        </button>
      </span>
    );
  }

  const commit = async () => {
    const parsed = Number(draft.trim());
    if (!draft.trim() || !Number.isInteger(parsed) || parsed < 0) {
      setError("Enter a whole number of people, 0 or more.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await onSave(parsed);
      setEditing(false);
    } catch {
      setError("Could not save that. Try again.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <span className="active-users-field">
      <label className="active-users-label" htmlFor="active-users-input">
        Active users in {label}
      </label>
      <input
        id="active-users-input"
        ref={inputRef}
        className="active-users-input"
        type="number"
        min={0}
        step={1}
        value={draft}
        disabled={busy}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") commit();
          if (e.key === "Escape") setEditing(false);
        }}
      />
      <button type="button" className="link" onClick={commit} disabled={busy}>
        {busy ? "Saving…" : "Save"}
      </button>
      <button type="button" className="link" onClick={() => setEditing(false)} disabled={busy}>
        Cancel
      </button>
      {error && (
        <span className="error active-users-error" role="alert">
          {error}
        </span>
      )}
    </span>
  );
}
