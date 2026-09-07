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
import { findTopic } from "../help/content";
import { retrieve } from "../help/retrieve";

interface Message {
  role: "user" | "assistant";
  content: string;
  sources?: string[];
  /** Set on a reply the handbook could not answer, so the UI can offer a human. */
  unanswered?: boolean;
}

const STORE_KEY = "meter.assistant.thread";

const GREETING =
  "Hi 👋 I'm the Meter assistant. Ask me anything about how the numbers are " +
  "put together, connecting a provider, or getting set up.";

const SUGGESTIONS = [
  "What's the difference between build and inference cost?",
  "Why is spend showing as unattributed?",
  "How do I connect a provider?",
  "How does feature discovery work?",
  "What is confidence on a cost row?",
  "Is my billing data secure?",
];

/** A human-readable name for the screen the user is on, for context. */
function pageLabel(pathname: string): string {
  const first = pathname.split("/").filter(Boolean)[0];
  const labels: Record<string, string> = {
    optimize: "Optimize",
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

/** The handbook topics an answer was drawn from, as links. */
function Sources({ ids }: { ids: string[] }) {
  const found = ids
    .map((id) => {
      const [category, topic] = id.split("/");
      const hit = category && topic ? findTopic(category, topic) : undefined;
      return hit ? { id, title: hit.topic.title } : null;
    })
    .filter((x): x is { id: string; title: string } => x !== null);
  if (!found.length) return null;
  return (
    <div className="assist-sources">
      <span className="assist-sources-label">In the handbook</span>
      {found.map((source) => (
        <Link key={source.id} to={`/help/${source.id}`} className="assist-source">
          {source.title} →
        </Link>
      ))}
    </div>
  );
}

export function Assistant() {
  const [open, setOpen] = useState(false);
  const [thread, setThread] = useState<Message[]>(loadThread);
  const [draft, setDraft] = useState("");
  const [pending, setPending] = useState(false);
  const [supportEmail, setSupportEmail] = useState("");
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

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [thread, pending, open]);

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
        setThread((prior) => [
          ...prior,
          {
            role: "assistant",
            content: reply.answer,
            sources: reply.sources,
            unanswered: !reply.answered,
          },
        ]);
      } catch (error) {
        const message =
          error instanceof ApiError && error.status === 429
            ? "That's a lot of questions at once — give it a minute and try again."
            : "Something went wrong reaching the assistant. Try again in a moment.";
        setThread((prior) => [...prior, { role: "assistant", content: message, unanswered: true }]);
      } finally {
        setPending(false);
      }
    },
    [location.pathname, pending, thread],
  );

  const asked = useMemo(() => thread.some((m) => m.role === "user"), [thread]);

  return (
    <>
      {open && (
        <div className="assist-panel" role="dialog" aria-label="Meter support assistant">
          <header className="assist-head">
            <div>
              <p className="assist-title">Support</p>
              <p className="assist-sub">
                {composed ? "Answers from the Meter handbook" : "Straight from the handbook"}
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
              <div
                key={i}
                className={message.role === "user" ? "assist-msg user" : "assist-msg bot"}
              >
                {message.content.split("\n\n").map((para, j) => (
                  <p key={j}>
                    <Inline text={para} />
                  </p>
                ))}
                {message.sources && message.sources.length > 0 && <Sources ids={message.sources} />}
                {message.unanswered && supportEmail && (
                  <a className="assist-source" href={`mailto:${supportEmail}`}>
                    Email support →
                  </a>
                )}
              </div>
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
            {!asked && (
              <>
                <p className="assist-label">Common questions</p>
                <div className="assist-chips">
                  {SUGGESTIONS.map((question) => (
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
            <div className="assist-actions">
              {supportEmail && (
                <a className="assist-btn" href={`mailto:${supportEmail}`}>
                  Contact support
                </a>
              )}
              <Link className="assist-btn ghost" to="/help" onClick={() => setOpen(false)}>
                Knowledge base
              </Link>
            </div>
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
