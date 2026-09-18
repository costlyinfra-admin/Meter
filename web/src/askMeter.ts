/**
 * The "Ask Meter" discovery bubble: what it remembers, and how it talks to the
 * chat that already exists.
 *
 * Three small jobs, kept out of the components so the bubble, the inline
 * actions and the assistant all agree on them:
 *
 *   **What the reader has already seen.** Stored the way `theme.ts` stores a
 *   theme — one key, read and written inside try/catch, because private mode
 *   and blocked storage are normal and must not throw into a render.
 *
 *   **How anything opens the chat.** A window event, the same pattern the
 *   alerts badge uses (`REFRESH_ALERTS_EVENT`). It means a button beside a
 *   heading three components deep can open the assistant and ask it something
 *   without prop-drilling or lifting state out of it.
 *
 *   **Counting, once.** An impression that fires on every re-render is not an
 *   impression, so the one-shot events go through `trackOnce`.
 */
import { track } from "./observability/datadog";

/** Set the first time someone opens or uses the chat. The bubble is for people
 *  who have not found it; after that it would be noise. */
export const DISCOVERED_KEY = "meter.ask.discovered";
/** Set when the expanded invitation has been shown. It is a one-time offer. */
export const INVITED_KEY = "meter.ask.invited";
/** Set when the bubble itself is closed. Asked to go away, it stays away. */
export const DISMISSED_KEY = "meter.ask.dismissed";

/** Anything may dispatch this to open the chat, optionally with a question. */
export const ASK_EVENT = "meter:ask-assistant";
/** The Overview publishes what it is currently showing, for the invitation. */
export const OVERVIEW_CONTEXT_EVENT = "meter:overview-context";
/** The chat announces that it was opened, however it was opened. */
export const CHAT_OPENED_EVENT = "meter:chat-opened";

export interface AskDetail {
  /** Empty opens the chat without asking anything. */
  question: string;
  /** Where the ask came from, for analytics: "bubble", "insights", … */
  source: string;
}

/** One suggested question, with a line of context drawn from live data. */
export interface AskSuggestion {
  /** The button's own words, e.g. "Explain the cost spike". */
  label: string;
  /** What that means on this page, today — read off the live numbers. */
  detail: string;
  /** What the assistant is actually asked. */
  question: string;
  /** A noun for the invitation's one-line summary, e.g. "the cost spike". */
  topic: string;
}

function readFlag(key: string): boolean {
  try {
    return localStorage.getItem(key) === "1";
  } catch {
    // Private mode, or storage blocked. Treat it as "not seen": the bubble is
    // small and dismissible, and silently hiding it would be the worse failure.
    return false;
  }
}

function writeFlag(key: string): void {
  try {
    localStorage.setItem(key, "1");
  } catch {
    // It still applies for this session; it just won't be remembered.
  }
}

export const hasDiscoveredChat = (): boolean => readFlag(DISCOVERED_KEY);
export const hasSeenInvitation = (): boolean => readFlag(INVITED_KEY);
export const wasDismissed = (): boolean => readFlag(DISMISSED_KEY);
export const markChatDiscovered = (): void => writeFlag(DISCOVERED_KEY);
export const markInvitationSeen = (): void => writeFlag(INVITED_KEY);
export const markDismissed = (): void => writeFlag(DISMISSED_KEY);

/**
 * Forget everything the bubble remembers.
 *
 * For development and for tests. Also hung off `window` in a dev build, so the
 * flow can be replayed from the console without clearing site data by hand.
 */
export function resetAskMeter(): void {
  try {
    localStorage.removeItem(DISCOVERED_KEY);
    localStorage.removeItem(INVITED_KEY);
    localStorage.removeItem(DISMISSED_KEY);
  } catch {
    // Nothing stored means nothing to forget.
  }
  seen.clear();
  context = [];
}

const seen = new Set<string>();

/** Count an event that must only ever be counted once per page life. */
export function trackOnce(name: string, attributes?: Record<string, unknown>): void {
  if (seen.has(name)) return;
  seen.add(name);
  track(name, attributes);
}

/** Open the chat. With a question, the chat asks it; without, it just opens. */
export function askMeter(question: string, source: string): void {
  track("ask_meter_ask", { source, asked: Boolean(question) });
  window.dispatchEvent(new CustomEvent<AskDetail>(ASK_EVENT, { detail: { question, source } }));
}

/**
 * What the Overview is currently showing, as questions worth asking about it.
 *
 * Kept in the module as well as announced, because the bubble and the page that
 * supplies it are mounted independently: whichever arrives second still needs
 * the answer, and a page's data lands long after both of them have rendered.
 */
let context: AskSuggestion[] = [];

export function publishOverviewContext(suggestions: AskSuggestion[]): void {
  context = suggestions;
  window.dispatchEvent(
    new CustomEvent<AskSuggestion[]>(OVERVIEW_CONTEXT_EVENT, { detail: suggestions }),
  );
}

export const overviewContext = (): AskSuggestion[] => context;

if (import.meta.env.DEV && typeof window !== "undefined") {
  (window as unknown as { resetAskMeter: () => void }).resetAskMeter = resetAskMeter;
}
