/**
 * Settings → Privacy & data → "Prompt optimization".
 *
 * The one place an organization agrees to let Meter collect prompts, and the one
 * place it can take that back. Everything on the consent screen is something the
 * server holds the organization to: the text version and the named model are sent
 * back with the agreement, and the server refuses consent to a screen that no
 * longer matches what is true.
 *
 * Nothing here ever shows prompt content. Viewing captured prompts arrives with
 * the prompt screens (PO-3), behind its own audited request.
 */
import { useEffect, useState } from "react";
import { api, ApiError, type PromptAuditEvent, type PromptConsentStatus } from "../api";

const EVENT_LABELS: Record<string, string> = {
  consent_granted: "Turned on prompt optimization",
  consent_withdrawn: "Withdrew consent and deleted captured prompts",
  data_key_destroyed: "Destroyed the encryption key",
  feature_enabled: "Turned on capture for",
  feature_disabled: "Turned off capture for",
  sample_content_viewed: "Viewed a captured prompt for",
  samples_purged: "Deleted prompts older than the retention window",
};

const SHOWN_EVENTS = 20;

export function PromptOptimizationCard() {
  const [status, setStatus] = useState<PromptConsentStatus | null>(null);
  const [events, setEvents] = useState<PromptAuditEvent[]>([]);
  const [loadFailed, setLoadFailed] = useState(false);
  const [agreed, setAgreed] = useState(false);
  const [password, setPassword] = useState("");
  const [confirmingWithdraw, setConfirmingWithdraw] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    Promise.all([api.promptConsent(), api.promptAudit()])
      .then(([s, a]) => {
        if (!live) return;
        setStatus(s);
        setEvents(a.events);
      })
      .catch(() => live && setLoadFailed(true));
    return () => {
      live = false;
    };
  }, []);

  async function run(kind: string, fn: () => Promise<PromptConsentStatus>): Promise<boolean> {
    setBusy(kind);
    setError(null);
    try {
      setStatus(await fn());
      try {
        setEvents((await api.promptAudit()).events);
      } catch {
        // The change itself landed; a stale activity list is not worth an error.
      }
      return true;
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Something went wrong. Try again.");
      return false;
    } finally {
      setBusy(null);
    }
  }

  if (!status) {
    return (
      <section className="settings-card">
        <h2>Prompt optimization</h2>
        <p className="muted">
          {loadFailed ? "Could not load prompt optimization settings." : "Loading…"}
        </p>
      </section>
    );
  }

  const disclosure = status.current_disclosure;
  const needsConsent = !status.consent || status.version_outdated || status.disclosure_changed;

  async function grant() {
    if (!disclosure) return;
    const entered = password;
    // Never keep a password in state longer than the request that needs it.
    setPassword("");
    const ok = await run("grant", () =>
      api.grantPromptConsent({
        password: entered,
        accepted_version: status!.current_version,
        accepted_disclosure: disclosure,
      }),
    );
    if (ok) setAgreed(false);
  }

  async function withdraw() {
    if (await run("withdraw", () => api.withdrawPromptConsent())) setConfirmingWithdraw(false);
  }

  return (
    <section className="settings-card prompt-optimization-card">
      <h2>Prompt optimization</h2>
      <p className="muted settings-hint">
        Meter can suggest cheaper versions of the prompts your product uses, explain every change,
        and test them before recommending anything. To do that it has to collect prompts, which it
        otherwise never does. Nothing is collected until you agree here <strong>and</strong> your
        application is set up to send prompts to Meter.
      </p>

      {needsConsent ? (
        <>
          {status.consent && (
            <p className="hint" role="status">
              <strong>Capture is paused.</strong>{" "}
              {status.version_outdated
                ? "The terms below have changed since they were agreed to."
                : "The model that would see your prompts has changed."}{" "}
              Review and agree again to resume.
            </p>
          )}

          <ul className="consent-terms">
            <li>
              <strong>What is collected:</strong> the prompt instructions your developers wrote,
              plus a small sample of real inputs and outputs, only for the features you choose.
            </li>
            <li>
              <strong>What is never collected:</strong> tool calls and their results, images and
              files, or anything from features you have not chosen. Text your application pastes
              into a message is part of that message.
            </li>
            <li>
              <strong>How long:</strong> {status.retention_days} days, encrypted with a key unique
              to your organization.
            </li>
            <li>
              <strong>Who sees your prompts:</strong>{" "}
              {disclosure ? (
                <>
                  {disclosure.provider}, model <code>{disclosure.model}</code>.{" "}
                  {disclosure.source === "byok"
                    ? "This is your organization's own model, set under Bring your own key."
                    : "This is Meter's model, because your organization has not set its own under Bring your own key."}
                </>
              ) : (
                "no model is available to optimize prompts yet, so this cannot be turned on."
              )}
            </li>
            <li>
              <strong>Testing costs tokens:</strong> checking a proposed prompt replays real
              examples on your provider account. Nothing runs without your approval.
            </li>
            <li>
              <strong>Visibility:</strong> anyone in your organization can view captured prompts,
              and every view is logged below.
            </li>
            <li>
              <strong>Withdrawing</strong> deletes everything collected immediately, and destroys
              the key that could read it.
            </li>
          </ul>

          {/* htmlFor as well as wrapping: some accessibility trees name a wrapped
              checkbox by its value ("on") rather than by the sentence it agrees to. */}
          <label className="consent-agree" htmlFor="consent-agree">
            <input
              id="consent-agree"
              type="checkbox"
              checked={agreed}
              onChange={(e) => setAgreed(e.target.checked)}
              disabled={!disclosure}
            />
            <span>I agree to these terms on behalf of my organization.</span>
          </label>

          <div className="settings-field">
            <label htmlFor="consent-password">Your password</label>
            <input
              id="consent-password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              disabled={!disclosure}
            />
            <span className="settings-hint muted">
              Re-entering your password confirms this is a deliberate choice.
            </span>
          </div>

          <div className="settings-actions">
            <button
              onClick={() => void grant()}
              disabled={!disclosure || !agreed || !password || busy !== null}
            >
              {busy === "grant" ? "Turning on…" : "Turn on prompt optimization"}
            </button>
          </div>
        </>
      ) : (
        <>
          <p className="muted">
            On since {new Date(status.consent!.granted_at).toLocaleDateString()}, agreed by{" "}
            {status.consent!.granted_by}. Prompts are seen by {status.consent!.disclosed.provider} (
            <code>{status.consent!.disclosed.model}</code>) and kept {status.retention_days} days.
          </p>

          <h3>Features</h3>
          {status.features.length === 0 ? (
            <p className="muted">No features yet. Discover your features first.</p>
          ) : (
            <ul className="prompt-features">
              {status.features.map((f) => (
                <li key={f.feature_id}>
                  <label className="toggle">
                    <input
                      type="checkbox"
                      checked={f.enabled}
                      aria-label={`Capture prompts for ${f.name}`}
                      disabled={busy !== null}
                      onChange={() =>
                        void run(`feature:${f.feature_id}`, () =>
                          api.setPromptFeature(f.feature_id, !f.enabled),
                        )
                      }
                    />
                    <span>{f.name}</span>
                  </label>
                  <span className="muted">
                    {f.samples} {f.samples === 1 ? "sample" : "samples"}
                  </span>
                </li>
              ))}
            </ul>
          )}
          <p className="settings-hint muted">
            Turning a feature off deletes the prompts captured for it.
          </p>
        </>
      )}

      {status.consent && (
        <div className="consent-withdraw">
          {confirmingWithdraw ? (
            <>
              <p role="alert">
                This deletes every captured prompt and destroys the encryption key. It cannot be
                undone.
              </p>
              <div className="settings-actions">
                <button className="danger" onClick={() => void withdraw()} disabled={busy !== null}>
                  {busy === "withdraw" ? "Deleting…" : "Yes, withdraw and delete"}
                </button>
                <button className="secondary" onClick={() => setConfirmingWithdraw(false)}>
                  Cancel
                </button>
              </div>
            </>
          ) : (
            <button className="secondary" onClick={() => setConfirmingWithdraw(true)}>
              Withdraw consent and delete everything
            </button>
          )}
        </div>
      )}

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      <h3>Activity</h3>
      {events.length === 0 ? (
        <p className="muted">Nothing yet.</p>
      ) : (
        <ul className="prompt-audit">
          {events.slice(0, SHOWN_EVENTS).map((e, i) => (
            <li key={`${e.created_at}-${i}`}>
              <span>
                {EVENT_LABELS[e.event] ?? e.event}
                {e.feature_name ? ` ${e.feature_name}` : ""}
              </span>
              <span className="muted">
                {e.actor ?? "Meter"} · {new Date(e.created_at).toLocaleString()}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
