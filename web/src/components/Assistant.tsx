/**
 * The support assistant — a launcher and panel pinned to the bottom-right of
 * every signed-in page, matching the one on costlyinfra.com.
 *
 * How an answer is produced: the question is matched against the knowledge base
 * already shipped in this bundle (help/retrieve.ts), and the matching excerpts
 * go to the backend with the question. The model answers from those excerpts and
 * nothing else, and names which ones it used — so every reply ends in links to
 * the handbook topics behind it. That is the same rule the rest of the product
 * follows: a number, or an answer, that cannot show its evidence is not shown.
 *
 * The thread lives in sessionStorage, so navigating between pages mid-question
 * does not throw the conversation away.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { api, ApiError } from "../api";
import { Inline } from "../help/render";
import { retrieve } from "../help/retrieve";

interface Message {
  role: "user" | "assistant";
  content: string;
  sources?: string[];
  /** Set on a reply the handbook could not answer, so the UI can offer a human. */
  unanswered?: boolean;
}

// Versioned. A restored thread carries its own greeting, so changing the
// greeting without this leaves everyone who has opened the panel before reading
// the old one — including its old promise about what the assistant can answer.
const STORE_KEY = "meter.assistant.thread.v2";

/**
 * How a reply is revealed.
 *
 * A whole answer appearing at once reads as a lookup; revealing it reads as a
 * reply, and gives someone a beat to start at the top rather than hunting for
 * it. The rate is capped at both ends: short answers are not instant, and long
 * ones do not become a wait — a 600-character answer still lands inside four
 * seconds, because nobody wants to watch a paragraph arrive.
 */
const TYPE_TICK_MS = 25;
const TYPE_MAX_MS = 4000;

/** Progressively reveals `text`. Returns the visible part and whether it is done. */
function useTypewriter(text: string, active: boolean): [string, boolean] {
  const [shown, setShown] = useState(active ? "" : text);

  useEffect(() => {
    if (!active || !text) {
      setShown(text);
      return;
    }
    // Someone who has asked for less motion wants the answer, not the effect.
    const still = window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches;
    // Nobody is watching an animation on a tab they have switched away from,
    // and a background tab's timers are throttled hard enough that the reveal
    // would stall mid-sentence until they came back.
    if (still || document.hidden) {
      setShown(text);
      return;
    }

    const total = Math.min(TYPE_MAX_MS, text.length * TYPE_TICK_MS);
    const start = Date.now();
    let timer = 0;
    setShown("");

    const finish = () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onHidden);
      setShown(text);
    };
    // Leaving the tab ends the reveal rather than pausing it, so coming back
    // shows a finished answer instead of a half-written one.
    const onHidden = () => {
      if (document.hidden) finish();
    };

    timer = window.setInterval(() => {
      // How far the reveal should be by now, from the clock rather than from a
      // count of ticks — a throttled or skipped tick then costs nothing.
      const at = Math.ceil((text.length * (Date.now() - start)) / total);
      if (at >= text.length) finish();
      else setShown(text.slice(0, at));
    }, TYPE_TICK_MS);
    document.addEventListener("visibilitychange", onHidden);

    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onHidden);
    };
  }, [text, active]);

  return [shown, shown.length >= text.length];
}

const GREETING =
  "Hi 👋 Ask me anything about your AI usage, costs, traces, agents, data " +
  "freshness, integrations, or how Meter works";

/** Starters that show the two halves it can answer from: this tenant's live
 *  data, and the handbook behind how a number is built. */
const SUGGESTIONS = [
  "Is an agent stuck in a loop?",
  "When was my data last refreshed?",
  "What caused yesterday's cost spike?",
  "Which traces used the most tokens?",
  "How is this number calculated?",
];

/** A human-readable name for the screen the user is on, for context. */
function pageLabel(pathname: string): string {
  // Prompts lives under /optimize but is its own screen in the nav.
  if (pathname.startsWith("/optimize/prompts")) return "Prompts";
  const first = pathname.split("/").filter(Boolean)[0];
  const labels: Record<string, string> = {
    optimize: "Recommendations",
    "cost-sources": "Cost sources",
    features: "Features",
    "install-sdk": "Install SDK",
    alerts: "Alerts",
    settings: "Settings",
    help: "Knowledge base",
  };
  return first ? (labels[first] ?? "") : "Overview";
}

function loadThread(): Message[] {
  try {
    const raw = sessionStorage.getItem(STORE_KEY);
    const parsed = raw ? JSON.parse(raw) : null;
    if (Array.isArray(parsed) && parsed.length) return parsed as Message[];
  } catch {
    // A corrupt or unavailable store just means starting fresh.
  }
  try {
    sessionStorage.removeItem("meter.assistant.thread"); // the pre-v2 thread
  } catch {
    // A corrupt or unavailable store just means starting fresh.
  }
  return [{ role: "assistant", content: GREETING }];
}

function ChatIcon() {
  return (
    <svg viewBox="0 0 24 24" width="24" height="24" aria-hidden fill="none" stroke="currentColor">
      <path
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M20 12a8 8 0 0 1-11.6 7.1L4 20l.9-4.4A8 8 0 1 1 20 12Z"
      />
      <path strokeWidth="1.8" strokeLinecap="round" d="M8.5 11h7M8.5 14h4" />
    </svg>
  );
}

function MailIcon() {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden fill="none" stroke="currentColor">
      <path
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M3 7a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"
      />
      <path strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" d="m3.5 7.5 8.5 6 8.5-6" />
    </svg>
  );
}

/**
 * The same open book the sidebar uses for Knowledge base (AppShell's `help`
 * glyph, verbatim). Both lead to /help, so they should look like the same door
 * rather than two different places that happen to share a destination.
 */
function BookIcon() {
  return (
    <svg
      viewBox="0 0 20 20"
      width="17"
      height="17"
      aria-hidden
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M10 5.6S8.4 3.4 3 3.4v10.9c5.4 0 7 2.2 7 2.2s1.6-2.2 7-2.2V3.4c-5.4 0-7 2.2-7 2.2Z" />
      <path d="M10 5.6v10.9" />
    </svg>
  );
}

function CloseIcon() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden fill="none" stroke="currentColor">
      <path strokeWidth="2" strokeLinecap="round" d="M6 6l12 12M18 6L6 18" />
    </svg>
  );
}

function SendIcon() {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden fill="none" stroke="currentColor">
      <path
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M4 12h14M13 6l6 6-6 6"
      />
    </svg>
  );
}

/**
 * One message. A reply being revealed holds back its sources and its support
 * link until the text has finished: they are the end of an answer, and having
 * them sit under a half-written sentence reads as though it had already
 * given up.
 */
function Bubble({
  message,
  typing,
  supportEmail,
  onTyped,
}: {
  message: Message;
  typing: boolean;
  supportEmail: string;
  onTyped: () => void;
}) {
  const [shown, done] = useTypewriter(message.content, typing && message.role === "assistant");

  useEffect(() => {
    if (typing && done) onTyped();
  }, [typing, done, onTyped]);

  return (
    <div className={message.role === "user" ? "assist-msg user" : "assist-msg bot"}>
      {shown.split("\n\n").map((para, j) => (
        <p key={j}>
          <Inline text={para} />
        </p>
      ))}
      {done && message.unanswered && supportEmail && (
        <a className="assist-source" href={`mailto:${supportEmail}`}>
          Email support →
        </a>
      )}
    </div>
  );
}

export function Assistant() {
  const [open, setOpen] = useState(false);
  const [thread, setThread] = useState<Message[]>(loadThread);
  const [draft, setDraft] = useState("");
  const [pending, setPending] = useState(false);
  // The index of the reply currently being revealed. Only the newest one types:
  // a thread restored from storage is history, and history does not type itself.
  const [typingAt, setTypingAt] = useState<number | null>(null);
  const [supportEmail, setSupportEmail] = useState("");
  /** Shown after the support icon is clicked; see `contactSupport`. */
  const [emailNote, setEmailNote] = useState("");
  const [composed, setComposed] = useState(true);
  const location = useLocation();
  const inputRef = useRef<HTMLInputElement>(null);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    try {
      sessionStorage.setItem(STORE_KEY, JSON.stringify(thread.slice(-40)));
    } catch {
      // Storage full or blocked; the thread simply won't survive a reload.
    }
  }, [thread]);

  // Loaded once the panel is first opened, not on every page load: nothing here
  // is needed until someone actually asks something.
  useEffect(() => {
    if (!open || supportEmail) return;
    api
      .assistantMeta()
      .then((meta) => {
        setSupportEmail(meta.support_email);
        setComposed(meta.composed);
      })
      .catch(() => setSupportEmail(""));
  }, [open, supportEmail]);

  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);

  const asked = useMemo(() => thread.some((m) => m.role === "user"), [thread]);

  /** Starters minus anything already asked — offering the same question twice
   *  reads as though the first answer didn't count. */
  const starters = useMemo(() => {
    const spent = new Set(
      thread.filter((m) => m.role === "user").map((m) => m.content.trim().toLowerCase()),
    );
    return SUGGESTIONS.filter((question) => !spent.has(question.toLowerCase()));
  }, [thread]);

  // Starters are a way back in, not only a way to start: they come back once a
  // reply has landed and finished revealing itself. They stay out of the way
  // while an answer is still arriving, so they never look like part of it.
  const showStarters = starters.length > 0 && !pending && typingAt === null;

  // Starters returning grows the footer and shortens the thread, which would
  // otherwise push the answer someone is still reading up out of sight — so
  // their arrival scrolls the thread too, not just a new message.
  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [thread, pending, open, showStarters]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  const ask = useCallback(
    async (question: string) => {
      const text = question.trim();
      if (!text || pending) return;
      const history = thread
        .slice(-6)
        .map(({ role, content }) => ({ role, content }))
        .filter((turn) => turn.content !== GREETING);
      setThread((prior) => [...prior, { role: "user", content: text }]);
      setDraft("");
      setPending(true);
      try {
        const reply = await api.askAssistant({
          question: text,
          passages: retrieve(text),
          history,
          page: pageLabel(location.pathname),
        });
        setThread((prior) => {
          setTypingAt(prior.length);
          return [
            ...prior,
            {
              role: "assistant",
              content: reply.answer,
              sources: reply.sources,
              unanswered: !reply.answered,
            },
          ];
        });
      } catch (error) {
        const message =
          error instanceof ApiError && error.status === 429
            ? "That's a lot of questions at once — give it a minute and try again."
            : "Something went wrong reaching the assistant. Try again in a moment.";
        setThread((prior) => {
          setTypingAt(prior.length);
          return [...prior, { role: "assistant", content: message, unanswered: true }];
        });
      } finally {
        setPending(false);
      }
    },
    [location.pathname, pending, thread],
  );

  /**
   * mailto: is handed to the operating system, and a browser with no mail
   * handler registered drops it in silence — which is exactly what "the email
   * icon does not work" looks like. The href stays (it is right for everyone
   * who does have a mail client, and keeps right-click → copy address), but the
   * click also puts the address on the clipboard and says so, so it always
   * leaves someone with a way to write.
   */
  const contactSupport = useCallback(() => {
    const say = (note: string) => setEmailNote(note);
    try {
      navigator.clipboard.writeText(supportEmail).then(
        () => say(`${supportEmail} — copied`),
        () => say(supportEmail),
      );
    } catch {
      say(supportEmail); // no clipboard here; showing it is still enough to use
    }
  }, [supportEmail]);

  return (
    <>
      {open && (
        <div className="assist-panel" role="dialog" aria-label="Meter support assistant">
          <header className="assist-head">
            <div>
              <p className="assist-title">Ask Meter</p>
              <p className="assist-sub">
                {composed ? "Your data and the Meter handbook" : "Straight from the handbook"}
              </p>
            </div>
            <button
              className="assist-x"
              onClick={() => setOpen(false)}
              aria-label="Close assistant"
            >
              <CloseIcon />
            </button>
          </header>

          <div className="assist-thread" aria-live="polite">
            {thread.map((message, i) => (
              <Bubble
                key={i}
                message={message}
                typing={typingAt === i}
                supportEmail={supportEmail}
                onTyped={() => setTypingAt((at) => (at === i ? null : at))}
              />
            ))}
            {pending && (
              <div className="assist-msg bot assist-typing" aria-label="Thinking">
                <span />
                <span />
                <span />
              </div>
            )}
            <div ref={endRef} />
          </div>

          <div className="assist-foot">
            {showStarters && (
              <>
                <p className="assist-label">{asked ? "Ask something else" : "Common questions"}</p>
                <div className="assist-chips">
                  {starters.map((question) => (
                    <button key={question} className="assist-chip" onClick={() => ask(question)}>
                      {question}
                    </button>
                  ))}
                </div>
              </>
            )}
            <form
              className="assist-form"
              onSubmit={(e) => {
                e.preventDefault();
                ask(draft);
              }}
            >
              <input
                ref={inputRef}
                className="assist-input"
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                placeholder="Ask your own question…"
                aria-label="Ask the assistant a question"
                maxLength={1000}
              />
              <button
                className="assist-send"
                type="submit"
                disabled={pending || !draft.trim()}
                aria-label="Send question"
              >
                <SendIcon />
              </button>
            </form>
            {/* Icons, not buttons: these are secondary exits from a conversation,
                and two full-width buttons competed with the thing someone came
                here to do. The label survives as the accessible name and the
                tooltip, so nothing is lost to a screen reader. */}
            <div className="assist-actions">
              {supportEmail && (
                <a
                  className="assist-icon-btn"
                  href={`mailto:${supportEmail}`}
                  onClick={contactSupport}
                  aria-label="Contact support"
                  title={`Contact support — ${supportEmail}`}
                >
                  <MailIcon />
                </a>
              )}
              <Link
                className="assist-icon-btn"
                to="/help"
                onClick={() => setOpen(false)}
                aria-label="Knowledge base"
                title="Knowledge base"
              >
                <BookIcon />
              </Link>
            </div>
            {emailNote && (
              <p className="assist-note" aria-live="polite">
                {emailNote}
              </p>
            )}
          </div>
        </div>
      )}

      <button
        className="assist-fab"
        onClick={() => setOpen((was) => !was)}
        aria-expanded={open}
        aria-label={open ? "Close support assistant" : "Open support assistant"}
      >
        <span className="assist-fab-ripple" />
        <span className="assist-fab-icon">{open ? <CloseIcon /> : <ChatIcon />}</span>
      </button>
    </>
  );
}
