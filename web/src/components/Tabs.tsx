/**
 * A tablist with the keyboard behaviour the pattern actually calls for.
 *
 * The app already had tabs in three places — the Overview's breakdown, Settings,
 * the By Provider split — each hand-rolled with `role="tab"` and a click
 * handler and no arrow-key handling at all. That is fine to look at and
 * unusable from a keyboard: a real tablist is ONE tab stop, and Left/Right move
 * between tabs inside it, rather than every tab being its own stop.
 *
 * This is the version with that behaviour. It is used by Install SDK; the
 * existing tablists are left as they are, since changing them is a separate job
 * from this one.
 *
 * Panels are rendered by the caller and kept mounted, hidden with `hidden`, so
 * switching tabs neither loses what a panel was holding nor shifts the layout
 * as one unmounts and another measures itself.
 */
import { useRef, type KeyboardEvent, type ReactNode } from "react";

export interface TabItem<Id extends string> {
  id: Id;
  label: ReactNode;
  /** Announced name, when `label` is more than text (a logo beside a word). */
  accessibleLabel?: string;
}

export function Tabs<Id extends string>({
  items,
  active,
  onChange,
  /** Distinguishes ids when a page has more than one tablist. */
  idPrefix,
  label,
  className = "",
}: {
  items: TabItem<Id>[];
  active: Id;
  onChange: (id: Id) => void;
  idPrefix: string;
  /** Names the tablist for a screen reader. */
  label: string;
  className?: string;
}) {
  const refs = useRef(new Map<Id, HTMLButtonElement>());

  function focus(id: Id) {
    onChange(id);
    // Selection follows focus, which is the expected behaviour for tabs whose
    // panels are already in the document: arrowing along reveals each one.
    refs.current.get(id)?.focus();
  }

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    const index = items.findIndex((t) => t.id === active);
    if (index < 0) return;
    const last = items.length - 1;
    const target =
      event.key === "ArrowRight"
        ? items[index === last ? 0 : index + 1] // wraps, as the pattern expects
        : event.key === "ArrowLeft"
          ? items[index === 0 ? last : index - 1]
          : event.key === "Home"
            ? items[0]
            : event.key === "End"
              ? items[last]
              : null;
    if (!target) return;
    event.preventDefault();
    focus(target.id);
  }

  return (
    <div role="tablist" aria-label={label} className={`tabs ${className}`} onKeyDown={onKeyDown}>
      {items.map((item) => {
        const selected = item.id === active;
        return (
          <button
            key={item.id}
            type="button"
            role="tab"
            id={`${idPrefix}-tab-${item.id}`}
            aria-selected={selected}
            aria-controls={`${idPrefix}-panel-${item.id}`}
            // One tab stop for the whole list: Tab reaches the selected tab,
            // arrows move within it, Tab again leaves for the panel.
            tabIndex={selected ? 0 : -1}
            className={selected ? "tab active" : "tab"}
            aria-label={item.accessibleLabel}
            ref={(el) => {
              if (el) refs.current.set(item.id, el);
              else refs.current.delete(item.id);
            }}
            onClick={() => onChange(item.id)}
          >
            {item.label}
          </button>
        );
      })}
    </div>
  );
}

/** The panel for one tab. Kept mounted when inactive; `hidden` takes it away. */
export function TabPanel({
  id,
  idPrefix,
  active,
  children,
}: {
  id: string;
  idPrefix: string;
  active: boolean;
  children: ReactNode;
}) {
  return (
    <div
      role="tabpanel"
      id={`${idPrefix}-panel-${id}`}
      aria-labelledby={`${idPrefix}-tab-${id}`}
      hidden={!active}
      // Reachable by Tab from the tablist, which is how someone gets from the
      // tabs into what they select.
      tabIndex={active ? 0 : -1}
    >
      {children}
    </div>
  );
}
