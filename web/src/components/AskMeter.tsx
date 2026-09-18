/**
 * "Ask Meter" — the discovery layer over the chat that already exists.
 *
 * The assistant has been in the bottom-right corner of every page for months
 * and people do not click it, because a circle with a speech-bubble glyph does
 * not say what it knows. Nothing here replaces that launcher: the bubble sits
 * above it, points at it, and says out loud what it is for. Clicking anything
 * here opens the same panel, through the same `ask()`.
 *
 * Two things live in this file:
 *
 *   `AskMeterBubble` — the corner bubble. Compact by default; on the Overview
 *   it expands once, after a pause, into an invitation built from what that
 *   page is actually showing right now. It never expands a second time, never
 *   animates on a loop, and never opens the panel by itself.
 *
 *   `AskAction` — a small "Ask Meter" beside a panel heading or a metric, for
 *   the reader who is already looking at the thing they want explained.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation } from "react-router-dom";
import {
  ASK_EVENT,
  AskSuggestion,
  CHAT_OPENED_EVENT,
  OVERVIEW_CONTEXT_EVENT,
  askMeter,
  hasDiscoveredChat,
  hasSeenInvitation,
  markDismissed,
  markInvitationSeen,
  overviewContext,
  trackOnce,
  wasDismissed,
} from "../askMeter";
import { track } from "../observability/datadog";

/** Long enough that the page has been read, short enough to still be about it. */
const INVITE_AFTER_MS = 6000;

function SparkIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <path d="M12 2l1.9 5.6L19.5 9l-5.6 1.9L12 16.5l-1.9-5.6L4.5 9l5.6-1.4L12 2z" />
      <path d="M18.5 15l.9 2.6 2.6.9-2.6.9-.9 2.6-.9-2.6-2.6-.9 2.6-.9.9-2.6z" opacity=".6" />
    </svg>
  );
}

function CloseIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M6 6l12 12M18 6L6 18"
        stroke="currentColor"
        strokeWidth="2.5"
        strokeLinecap="round"
      />
    </svg>
  );
}

/**
 * The sentence under the invitation's heading, naming only what was found.
 *
 * Written from the suggestions rather than fixed, so it can never promise an
 * explanation of a spike on a page that has no spike.
 */
function invitationLead(suggestions: AskSuggestion[]): string {
  const topics = suggestions.map((s) => s.topic);
  if (topics.length === 0) return "";
  const list =
    topics.length === 1
      ? topics[0]
      : `${topics.slice(0, -1).join(", ")}, or ${topics[topics.length - 1]}`;
  return `Ask me about ${list}.`;
}

export function AskMeterBubble() {
  const location = useLocation();
  // Read once, at mount: someone who opened the chat in this session should not
  // have the bubble vanish out from under the cursor mid-click.
  const [hidden, setHidden] = useState(() => hasDiscoveredChat() || wasDismissed());
  const [expanded, setExpanded] = useState(false);
  const [suggestions, setSuggestions] = useState<AskSuggestion[]>(overviewContext);
  const headingRef = useRef<HTMLParagraphElement>(null);
  const onOverview = location.pathname === "/";

  // Whatever opens the chat — this bubble, an inline action, or the launcher
  // itself — retires the bubble. It exists to point at a door nobody had opened.
  useEffect(() => {
    const found = () => setHidden(true);
    window.addEventListener(CHAT_OPENED_EVENT, found);
    return () => window.removeEventListener(CHAT_OPENED_EVENT, found);
  }, []);

  useEffect(() => {
    const onContext = (e: Event) =>
      setSuggestions((e as CustomEvent<AskSuggestion[]>).detail ?? []);
    window.addEventListener(OVERVIEW_CONTEXT_EVENT, onContext);
    return () => window.removeEventListener(OVERVIEW_CONTEXT_EVENT, onContext);
  }, []);

  useEffect(() => {
    if (hidden) return;
    trackOnce("ask_meter_impression");
  }, [hidden]);

  // The invitation: once per reader, only on the Overview, only once that page
  // has something specific to offer, and only after they have had time to look
  // at it. The timer restarts if they arrive with no data yet and it lands late.
  useEffect(() => {
    if (hidden || expanded || !onOverview) return;
    if (suggestions.length === 0 || hasSeenInvitation()) return;
    const timer = window.setTimeout(() => {
      setExpanded(true);
      markInvitationSeen();
      track("ask_meter_expanded", { suggestions: suggestions.length });
    }, INVITE_AFTER_MS);
    return () => window.clearTimeout(timer);
  }, [hidden, expanded, onOverview, suggestions]);

  // Moving focus would steal the cursor out of whatever the reader was doing;
  // announcing it lets a screen reader reach the offer at its own pace.
  useEffect(() => {
    if (expanded) headingRef.current?.focus({ preventScroll: true });
  }, [expanded]);

  const lead = useMemo(() => invitationLead(suggestions), [suggestions]);

  const dismiss = useCallback(() => {
    track("ask_meter_dismissed", { state: expanded ? "expanded" : "compact" });
    if (expanded) {
      // Dismissing the offer declines the offer, not the assistant: it falls
      // back to the compact bubble rather than disappearing.
      setExpanded(false);
      return;
    }
    markDismissed();
    setHidden(true);
  }, [expanded]);

  if (hidden) return null;

  if (!expanded) {
    return (
      <div className="ask-bubble ask-bubble-compact">
        <button type="button" className="ask-bubble-open" onClick={() => askMeter("", "bubble")}>
          <SparkIcon />
          <span>Ask Meter</span>
        </button>
        <button
          type="button"
          className="ask-bubble-close"
          onClick={dismiss}
          aria-label="Dismiss the Ask Meter tip"
        >
          <CloseIcon />
        </button>
      </div>
    );
  }

  return (
    <div
      className="ask-bubble ask-bubble-expanded"
      role="region"
      aria-labelledby="ask-bubble-heading"
    >
      <div className="ask-bubble-head">
        <span className="ask-bubble-mark">
          <SparkIcon />
          Ask Meter
        </span>
        <button
          type="button"
          className="ask-bubble-close"
          onClick={dismiss}
          aria-label="Dismiss the Ask Meter suggestions"
        >
          <CloseIcon />
        </button>
      </div>
      <p className="ask-bubble-title" id="ask-bubble-heading" tabIndex={-1} ref={headingRef}>
        I found a few things worth investigating
      </p>
      <p className="ask-bubble-lead">{lead}</p>
      <ul className="ask-bubble-prompts">
        {suggestions.map((s) => (
          <li key={s.label}>
            <button
              type="button"
              className="ask-bubble-prompt"
              onClick={() => askMeter(s.question, "invitation")}
            >
              <span className="ask-bubble-prompt-label">{s.label}</span>
              <span className="ask-bubble-prompt-detail">{s.detail}</span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

/**
 * "Ask Meter" beside something on the page, carrying that thing's question.
 *
 * `source` is what gets counted; `question` is what the assistant is asked.
 */
export function AskAction({
  question,
  source,
  label = "Ask Meter",
}: {
  question: string;
  source: string;
  label?: string;
}) {
  return (
    <button
      type="button"
      className="ask-action"
      onClick={() => askMeter(question, source)}
      title={question}
    >
      <SparkIcon />
      <span>{label}</span>
    </button>
  );
}

export { ASK_EVENT };
